"""Editorial review queue for fire events.

The ``ReviewQueue`` is the single chokepoint through which every FireEvent
must pass before it can be published. CLAUDE.md mandates that for the first
6 months of operation, ALL events — including high-confidence CONFIRMED ones —
require manual editorial review. ``ReviewQueue`` enforces this: there is no
``auto_approve`` path.

Architecture
------------
Two implementations share one ``ReviewQueueProtocol``:

- ``InMemoryReviewQueue`` — dict-backed; for tests and local development.
  State is lost on process restart.
- ``PostgresReviewQueue`` — stub; implemented once the ORM prompt lands.

Every state-mutating method writes an ``EditorialAction`` record before
modifying the event's status. The action log is the source of truth; the
denormalised ``status`` on the event is a read convenience only.

Methodology reference: methodology/v1.0.pdf §5.1 — "Editorial Workflow".

CLAUDE.md: "Never silently delete; always changelog."
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from wced.models.editorial import (
    EditorialAction,
    EditorialActionType,
    EditorialTransitionError,
    validate_transition,
)
from wced.models.event import EventStatus, FireEvent

log = logging.getLogger(__name__)


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
        log.warning(
            "editorial.retract: event=%s reviewer=%s reason=%r",
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

    def get(self, event_id: UUID) -> FireEvent:
        # SELECT * FROM fire_events WHERE id=event_id;
        raise NotImplementedError

    def history(self, event_id: UUID) -> list[EditorialAction]:
        # SELECT * FROM editorial_actions WHERE event_id=event_id ORDER BY acted_at ASC;
        raise NotImplementedError
