"""Editorial review queue for fire events.

The ``ReviewQueue`` is the single chokepoint through which every FireEvent
must pass before it can be published. The v2 confidence-gated auto-publish
policy allows auto-publishing Confirmed/Verified events once the publish gate
is merged; until then, ``--no-auto-publish`` is enforced in the Justfile.

Architecture
------------
Two implementations share one ``ReviewQueueProtocol``:

- ``InMemoryReviewQueue`` — dict-backed; for tests and local development.
  State is lost on process restart.
- ``PostgresReviewQueue`` — stub; implemented once the ORM prompt lands.

Every state-mutating method writes an ``EditorialAction`` record AND appends
to the ``publication_log`` (append-only audit trail). The action log is the
source of truth; the denormalised ``status`` on the event is a read
convenience only.

Methodology reference: methodology/v1.0.pdf §5.1 — "Editorial Workflow".

CLAUDE.md: "Never silently delete; always changelog."
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from wced.models.editorial import (
    EditorialAction,
    EditorialActionType,
    EditorialTransitionError,
    validate_transition,
)
from wced.models.event import EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord, Source
from wced.settings import get_settings

if TYPE_CHECKING:
    from wced.quantify.distribution import Distribution
    from wced.quantify.reconcile import ReconciliationResult
    from wced.provenance.store import ProvenanceStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Confidence-gated publish gate (CLAUDE.md §"Confidence-Gated Auto-Publish")
# ---------------------------------------------------------------------------

_AUTO_PUBLISH_LABELS = frozenset({ConfidenceLabel.CONFIRMED, ConfidenceLabel.VERIFIED})
_HOLD_LABELS = frozenset({
    ConfidenceLabel.REPORTED,
    ConfidenceLabel.SUSPECTED,
    ConfidenceLabel.CLAIMED,
})
_MIN_MC_SAMPLES = 10_000


class PublishDecision(BaseModel):
    """Result of ``publish_gate``: publish, hold, or reject with a reason."""

    model_config = ConfigDict(frozen=True)

    action: Literal["publish", "hold", "reject"]
    reason: str | None = None


def publish_gate(
    event: FireEvent,
    distribution: "Distribution",
    provenance_store: "ProvenanceStore",
    *,
    reconciliation: "ReconciliationResult | None" = None,
    cross_method_tolerance: float | None = None,
) -> PublishDecision:
    """Evaluate the four publish gates and return a routing decision.

    Gate evaluation order (first failure wins):
      1. **Provenance gate** — event.provenance_id must resolve to a complete
         chain (at least one Source leaf) in *provenance_store*.
      2. **Distribution gate** — *distribution* must carry ≥10,000 MC samples.
      3. **Confidence gate** — Confirmed/Verified → publish;
         Reported/Suspected/Claimed → hold for manual review.
      4. **Cross-method gate** — when a *reconciliation* result is supplied,
         a bottom-up (inventory) vs top-down (FRP) divergence beyond tolerance
         downgrades an otherwise auto-publishable event to ``hold`` so an
         editor can resolve the discrepancy (CLAUDE.md gate #4, methodology
         §3.5). This gate runs only after the confidence gate would publish —
         a low-confidence event is already held and a rejected event already
         rejected, so cross-method divergence cannot make either worse.

    Parameters
    ----------
    event : FireEvent
        The event being evaluated. Must be in PENDING_REVIEW status.
    distribution : Distribution
        The emission estimate distribution for this event.
    provenance_store : ProvenanceStore
        Store to look up the provenance chain for *event.provenance_id*.
    reconciliation : ReconciliationResult or None
        The FRP/inventory reconciliation for this event (from
        ``wced.quantify.reconcile``). When None, the cross-method gate is
        skipped — single-method events and callers that have not run
        reconciliation behave exactly as before.
    cross_method_tolerance : float or None
        Maximum acceptable agreement ratio ρ = p50(inventory) / p50(FRP) for
        auto-publish; the event must satisfy ``1/tol ≤ ρ ≤ tol``. When None,
        falls back to ``Settings.cross_method_tolerance`` (env
        ``WCED_CROSS_METHOD_TOLERANCE``, default 2.0). Must be > 0.

    Returns
    -------
    PublishDecision
        ``action="publish"`` — auto-approve.
        ``action="hold"``    — leave in PENDING_REVIEW for manual editorial.
        ``action="reject"``  — reject with a mandatory reason.
    """
    # Avoid circular import at module level
    from wced.provenance.store import ProvenanceStore as _PS

    # --- Gate 1: Provenance ---
    try:
        chain = list(provenance_store.walk_upstream(event.provenance_id))
    except KeyError:
        return PublishDecision(
            action="reject",
            reason=(
                f"Provenance chain incomplete: no record found for "
                f"provenance_id={event.provenance_id}"
            ),
        )

    has_source = any(isinstance(n, Source) for n in chain)
    if not has_source:
        return PublishDecision(
            action="reject",
            reason=(
                f"Provenance chain has no upstream Source for "
                f"provenance_id={event.provenance_id}"
            ),
        )

    # --- Gate 2: Distribution sample count ---
    if distribution.samples is None:
        return PublishDecision(
            action="reject",
            reason="Distribution has no samples (samples=None); require ≥10,000",
        )
    if len(distribution.samples) < _MIN_MC_SAMPLES:
        return PublishDecision(
            action="reject",
            reason=(
                f"Distribution has {len(distribution.samples)} samples; "
                f"require ≥10,000"
            ),
        )

    # --- Gate 3: Confidence label ---
    if event.confidence_label not in _AUTO_PUBLISH_LABELS:
        return PublishDecision(
            action="hold",
            reason=(
                f"Confidence {event.confidence_label.value} requires manual "
                f"editorial review"
            ),
        )

    # --- Gate 4: Cross-method reconciliation (methodology §3.5) ---
    if reconciliation is not None:
        divergence_reason = _cross_method_divergence_reason(
            reconciliation, cross_method_tolerance
        )
        if divergence_reason is not None:
            return PublishDecision(action="hold", reason=divergence_reason)

    return PublishDecision(action="publish")


def _cross_method_divergence_reason(
    reconciliation: "ReconciliationResult",
    cross_method_tolerance: float | None,
) -> str | None:
    """Return a hold reason if bottom-up vs top-down diverge beyond tolerance.

    ρ = p50(inventory) / p50(FRP). The methods agree for auto-publish when
    ``1/tol ≤ ρ ≤ tol``. Two ways to fail:

    1. ``reconciliation.needs_review`` — reconcile already refused to promote
       a final distribution because ρ fell outside its agreement band
       (methodology §3.5). This is honoured regardless of *tol*.
    2. ρ is within reconcile's band but outside the (possibly stricter)
       auto-publish *tol* configured here.

    Returns None when the methods agree, or when only one method is available
    (``agreement_ratio is None`` — there is no ratio to test).
    """
    if reconciliation.needs_review:
        return (
            "Cross-method reconciliation flagged divergence beyond tolerance; "
            "routing to editorial review. "
            f"{reconciliation.review_reason}"
        )

    rho = reconciliation.agreement_ratio
    if rho is None:
        # Single-method estimate — nothing to reconcile.
        return None

    tol = (
        cross_method_tolerance
        if cross_method_tolerance is not None
        else get_settings().cross_method_tolerance
    )
    if tol <= 0:
        raise ValueError(f"cross_method_tolerance must be > 0; got {tol}")

    low = 1.0 / tol
    if rho < low or rho > tol:
        return (
            f"Bottom-up vs top-down divergence ρ={rho:.3f} outside auto-publish "
            f"tolerance [{low:.3f}, {tol:.3f}] (methodology §3.5); routing to "
            f"editorial review."
        )
    return None


@runtime_checkable
class ReviewQueueProtocol(Protocol):
    """Interface satisfied by every ReviewQueue backend."""

    def submit(self, event: FireEvent, *, reviewer: str = "system") -> FireEvent:
        """Add a new event to the queue in PENDING_REVIEW status."""
        ...

    def pending(self) -> list[FireEvent]:
        """Return all events currently in PENDING_REVIEW, oldest first."""
        ...

    def approve(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        notes: str | None = None,
    ) -> FireEvent:
        """Transition event from PENDING_REVIEW to PUBLISHED."""
        ...

    def reject(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> FireEvent:
        """Transition event from PENDING_REVIEW to REJECTED."""
        ...

    def resubmit(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        notes: str | None = None,
    ) -> FireEvent:
        """Return a REJECTED event to PENDING_REVIEW for a second review."""
        ...

    def retract(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> FireEvent:
        """Transition a PUBLISHED event to RETRACTED with a public changelog entry."""
        ...

    def flag_anomaly(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> FireEvent:
        """Auto-retract a PUBLISHED outlier to PENDING_REVIEW (anomaly-watch)."""
        ...

    def get(self, event_id: UUID) -> FireEvent:
        """Return the event with its current status; raises KeyError if absent."""
        ...

    def history(self, event_id: UUID) -> list[EditorialAction]:
        """Return all EditorialAction rows for an event, oldest first."""
        ...


class InMemoryReviewQueue:
    """Dict-backed ReviewQueue for tests and local development.

    Not thread-safe. State is lost on process restart.
    """

    def __init__(self) -> None:
        self._events: dict[UUID, FireEvent] = {}
        self._actions: dict[UUID, list[EditorialAction]] = {}
        self._publication_log: list[dict] = []

    @property
    def publication_log(self) -> list[dict]:
        """Return a copy of the publication log (append-only)."""
        return list(self._publication_log)

    # ------------------------------------------------------------------ public

    def submit(self, event: FireEvent, *, reviewer: str = "system") -> FireEvent:
        """Add an event to the queue; idempotent if already PENDING_REVIEW.

        If the event is brand-new it must have status PENDING_REVIEW (the
        canonical state for unseen events per CLAUDE.md). If it is already
        stored and already PENDING_REVIEW, this is a no-op and the stored
        event is returned unchanged.

        Parameters
        ----------
        event : FireEvent
            The event to submit. ``event.status`` must be PENDING_REVIEW.
        reviewer : str
            Who (or what component) submitted the event.

        Returns
        -------
        FireEvent
            The event as stored (may differ from the input if the event was
            already present and the input's status was inconsistent).

        Raises
        ------
        ValueError
            If ``event.status`` is not PENDING_REVIEW on initial submission.
        """
        if event.id in self._events:
            stored = self._events[event.id]
            if stored.status is EventStatus.PENDING_REVIEW:
                return stored
            # The event exists but is not pending — this is a logic error in
            # the caller; raise rather than silently overwriting.
            raise ValueError(
                f"Event {event.id} is already in the queue with status "
                f"{stored.status.value}; use resubmit() to return a REJECTED "
                "event to the queue."
            )

        if event.status is not EventStatus.PENDING_REVIEW:
            raise ValueError(
                f"New events must have status PENDING_REVIEW; "
                f"got {event.status.value} for event {event.id}."
            )

        self._events[event.id] = event
        self._actions[event.id] = []
        self._record_action(
            event_id=event.id,
            action_type=EditorialActionType.SUBMITTED,
            reviewer=reviewer,
            notes=None,
            previous_status=EventStatus.PENDING_REVIEW,
            new_status=EventStatus.PENDING_REVIEW,
        )
        log.info("editorial.submit: event=%s reviewer=%s", event.id, reviewer)
        return event

    def pending(self) -> list[FireEvent]:
        """Return PENDING_REVIEW events sorted by created_at ascending."""
        events = [
            ev for ev in self._events.values()
            if ev.status is EventStatus.PENDING_REVIEW
        ]
        events.sort(key=lambda e: e.created_at)
        return events

    def approve(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        notes: str | None = None,
    ) -> FireEvent:
        """Publish an event after editorial approval.

        Parameters
        ----------
        event_id : UUID
            Event to approve. Must be in PENDING_REVIEW.
        reviewer : str
            Name or identifier of the approving reviewer.
        notes : str or None
            Optional annotation (recommended for traceability).

        Returns
        -------
        FireEvent
            Updated event with status PUBLISHED.

        Raises
        ------
        KeyError
            If the event is not in the queue.
        EditorialTransitionError
            If the event is not in PENDING_REVIEW (e.g. already REJECTED).
        """
        event = self._require(event_id)
        next_status = validate_transition(
            event_id, event.status, EditorialActionType.APPROVED
        )
        updated = self._update_status(event, next_status)
        self._record_action(
            event_id=event_id,
            action_type=EditorialActionType.APPROVED,
            reviewer=reviewer,
            notes=notes,
            previous_status=event.status,
            new_status=next_status,
        )
        self._append_publication_log(
            target_id=event_id, from_state=event.status,
            to_state=next_status, action="approve", actor=reviewer,
        )
        log.info("editorial.approve: event=%s reviewer=%s", event_id, reviewer)
        return updated

    def reject(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> FireEvent:
        """Reject an event and record the reason.

        Parameters
        ----------
        event_id : UUID
            Event to reject. Must be in PENDING_REVIEW.
        reviewer : str
            Name or identifier of the rejecting reviewer.
        reason : str
            Mandatory explanation (non-empty). Stored in the action log and
            surfaced on the public changelog.

        Returns
        -------
        FireEvent
            Updated event with status REJECTED.

        Raises
        ------
        KeyError
            If the event is not in the queue.
        ValueError
            If ``reason`` is empty.
        EditorialTransitionError
            If the event is not in PENDING_REVIEW.
        """
        if not reason or not reason.strip():
            raise ValueError(f"reject() requires a non-empty reason for event {event_id}.")
        event = self._require(event_id)
        next_status = validate_transition(
            event_id, event.status, EditorialActionType.REJECTED
        )
        updated = self._update_status(event, next_status)
        self._record_action(
            event_id=event_id,
            action_type=EditorialActionType.REJECTED,
            reviewer=reviewer,
            notes=reason,
            previous_status=event.status,
            new_status=next_status,
        )
        self._append_publication_log(
            target_id=event_id, from_state=event.status,
            to_state=next_status, action="reject", actor=reviewer,
            reason=reason,
        )
        log.info(
            "editorial.reject: event=%s reviewer=%s reason=%r",
            event_id, reviewer, reason,
        )
        return updated

    def resubmit(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        notes: str | None = None,
    ) -> FireEvent:
        """Return a REJECTED event to PENDING_REVIEW for a second review pass.

        This is the only path from REJECTED back into the queue. Callers
        should document the reason for resubmission in ``notes``.

        Parameters
        ----------
        event_id : UUID
            Event to resubmit. Must be in REJECTED status.
        reviewer : str
            Who is requesting the resubmission.
        notes : str or None
            Optional explanation of what changed since the rejection.

        Returns
        -------
        FireEvent
            Updated event with status PENDING_REVIEW.

        Raises
        ------
        KeyError
            If the event is not in the queue.
        EditorialTransitionError
            If the event is not in REJECTED status.
        """
        event = self._require(event_id)
        next_status = validate_transition(
            event_id, event.status, EditorialActionType.RESUBMITTED
        )
        updated = self._update_status(event, next_status)
        self._record_action(
            event_id=event_id,
            action_type=EditorialActionType.RESUBMITTED,
            reviewer=reviewer,
            notes=notes,
            previous_status=event.status,
            new_status=next_status,
        )
        self._append_publication_log(
            target_id=event_id, from_state=event.status,
            to_state=next_status, action="resubmit", actor=reviewer,
        )
        log.info("editorial.resubmit: event=%s reviewer=%s", event_id, reviewer)
        return updated

    def retract(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> FireEvent:
        """Retract a published event and write a changelog entry.

        Retraction is irreversible — RETRACTED events cannot be transitioned
        to any other status. The ``reason`` is mandatory and forms the public
        changelog entry per CLAUDE.md.

        Parameters
        ----------
        event_id : UUID
            Event to retract. Must be in PUBLISHED status.
        reviewer : str
            Who authorised the retraction.
        reason : str
            Mandatory explanation (non-empty). This is surfaced publicly as
            the changelog entry.

        Returns
        -------
        FireEvent
            Updated event with status RETRACTED.

        Raises
        ------
        KeyError
            If the event is not in the queue.
        ValueError
            If ``reason`` is empty.
        EditorialTransitionError
            If the event is not in PUBLISHED status.
        """
        if not reason or not reason.strip():
            raise ValueError(f"retract() requires a non-empty reason for event {event_id}.")
        event = self._require(event_id)
        next_status = validate_transition(
            event_id, event.status, EditorialActionType.RETRACTED
        )
        updated = self._update_status(event, next_status)
        self._record_action(
            event_id=event_id,
            action_type=EditorialActionType.RETRACTED,
            reviewer=reviewer,
            notes=reason,
            previous_status=event.status,
            new_status=next_status,
        )
        self._append_publication_log(
            target_id=event_id, from_state=event.status,
            to_state=next_status, action="retract", actor=reviewer,
            reason=reason,
        )
        log.warning(
            "editorial.retract: event=%s reviewer=%s reason=%r",
            event_id, reviewer, reason,
        )
        return updated

    def flag_anomaly(
        self,
        event_id: UUID,
        *,
        reviewer: str,
        reason: str,
    ) -> FireEvent:
        """Auto-retract a published outlier to PENDING_REVIEW for re-review.

        This is the editorial action behind CLAUDE.md gate #5: the
        ``anomaly-watch`` agent flags an estimate that diverges from the
        facility's history or its cross-method estimate, returns it to the
        review queue, and attaches a public "under review" note. Unlike
        ``retract`` (which is terminal), an anomaly-flagged event can be
        re-approved once an editor resolves the discrepancy.

        Parameters
        ----------
        event_id : UUID
            Event to flag. Must be in PUBLISHED status.
        reviewer : str
            Component or person triggering the flag (e.g. "anomaly-watch").
        reason : str
            Mandatory explanation (non-empty). Surfaced publicly alongside the
            "under review" note.

        Returns
        -------
        FireEvent
            Updated event with status PENDING_REVIEW.

        Raises
        ------
        KeyError
            If the event is not in the queue.
        ValueError
            If ``reason`` is empty.
        EditorialTransitionError
            If the event is not in PUBLISHED status.
        """
        if not reason or not reason.strip():
            raise ValueError(
                f"flag_anomaly() requires a non-empty reason for event {event_id}."
            )
        event = self._require(event_id)
        next_status = validate_transition(
            event_id, event.status, EditorialActionType.ANOMALY_FLAGGED
        )
        updated = self._update_status(event, next_status)
        self._record_action(
            event_id=event_id,
            action_type=EditorialActionType.ANOMALY_FLAGGED,
            reviewer=reviewer,
            notes=reason,
            previous_status=event.status,
            new_status=next_status,
        )
        self._append_publication_log(
            target_id=event_id, from_state=event.status,
            to_state=next_status, action="anomaly_retract", actor=reviewer,
            reason=reason, public_note="under review",
        )
        log.warning(
            "editorial.flag_anomaly: event=%s reviewer=%s reason=%r",
            event_id, reviewer, reason,
        )
        return updated

    def get(self, event_id: UUID) -> FireEvent:
        """Return the current event state.

        Raises
        ------
        KeyError
            If the event has never been submitted.
        """
        return self._require(event_id)

    def history(self, event_id: UUID) -> list[EditorialAction]:
        """Return all actions for an event in chronological order.

        Raises
        ------
        KeyError
            If the event has never been submitted.
        """
        self._require(event_id)  # validate presence
        return list(self._actions[event_id])

    def __len__(self) -> int:
        return len(self._events)

    # ----------------------------------------------------------------- private

    def _append_publication_log(
        self,
        *,
        target_id: UUID,
        from_state: EventStatus,
        to_state: EventStatus,
        action: str,
        actor: str,
        reason: str | None = None,
        public_note: str | None = None,
    ) -> None:
        self._publication_log.append({
            "id": uuid4(),
            "target_type": "fire_event",
            "target_id": target_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
            "action": action,
            "actor": actor,
            "reason": reason,
            "public_note": public_note,
            "created_at": datetime.now(tz=UTC),
        })

    def _require(self, event_id: UUID) -> FireEvent:
        try:
            return self._events[event_id]
        except KeyError:
            raise KeyError(
                f"Event {event_id} is not in the review queue."
            ) from None

    def _update_status(self, event: FireEvent, new_status: EventStatus) -> FireEvent:
        now = datetime.now(tz=UTC)
        updated = event.model_copy(update={"status": new_status, "updated_at": now})
        self._events[event.id] = updated
        return updated

    def _record_action(
        self,
        *,
        event_id: UUID,
        action_type: EditorialActionType,
        reviewer: str,
        notes: str | None,
        previous_status: EventStatus,
        new_status: EventStatus,
    ) -> None:
        action = EditorialAction(
            event_id=event_id,
            action_type=action_type,
            reviewer=reviewer,
            notes=notes,
            previous_status=previous_status,
            new_status=new_status,
            acted_at=datetime.now(tz=UTC),
        )
        self._actions[event_id].append(action)


class PostgresReviewQueue:
    """PostgreSQL-backed ReviewQueue.

    Stub — implemented once the SQLAlchemy ORM / Alembic migrations are in
    place. The SQL intent for each method is documented inline so the database
    prompt can fill it in without rediscovering intent.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def submit(self, event: FireEvent, *, reviewer: str = "system") -> FireEvent:
        # INSERT INTO fire_events ... ON CONFLICT (id) DO NOTHING;
        # INSERT INTO editorial_actions (SUBMITTED) ...;
        raise NotImplementedError

    def pending(self) -> list[FireEvent]:
        # SELECT * FROM fire_events WHERE status = 'PENDING_REVIEW'
        # ORDER BY created_at ASC;
        raise NotImplementedError

    def approve(self, event_id: UUID, *, reviewer: str, notes: str | None = None) -> FireEvent:
        # UPDATE fire_events SET status='PUBLISHED', updated_at=now() WHERE id=event_id;
        # INSERT INTO editorial_actions (APPROVED) ...;
        raise NotImplementedError

    def reject(self, event_id: UUID, *, reviewer: str, reason: str) -> FireEvent:
        # UPDATE fire_events SET status='REJECTED', updated_at=now() WHERE id=event_id;
        # INSERT INTO editorial_actions (REJECTED) ...;
        raise NotImplementedError

    def resubmit(self, event_id: UUID, *, reviewer: str, notes: str | None = None) -> FireEvent:
        # UPDATE fire_events SET status='PENDING_REVIEW', updated_at=now() WHERE id=event_id;
        # INSERT INTO editorial_actions (RESUBMITTED) ...;
        raise NotImplementedError

    def retract(self, event_id: UUID, *, reviewer: str, reason: str) -> FireEvent:
        # UPDATE fire_events SET status='RETRACTED', updated_at=now() WHERE id=event_id;
        # INSERT INTO editorial_actions (RETRACTED) ...;
        raise NotImplementedError

    def flag_anomaly(self, event_id: UUID, *, reviewer: str, reason: str) -> FireEvent:
        # UPDATE fire_events SET status='PENDING_REVIEW', updated_at=now() WHERE id=event_id;
        # INSERT INTO editorial_actions (ANOMALY_FLAGGED) ...;
        # INSERT INTO publication_log (action='anomaly_retract', public_note='under review') ...;
        raise NotImplementedError

    def get(self, event_id: UUID) -> FireEvent:
        # SELECT * FROM fire_events WHERE id=event_id;
        raise NotImplementedError

    def history(self, event_id: UUID) -> list[EditorialAction]:
        # SELECT * FROM editorial_actions WHERE event_id=event_id ORDER BY acted_at ASC;
        raise NotImplementedError
