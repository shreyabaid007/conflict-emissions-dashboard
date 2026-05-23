"""Editorial workflow domain models.

Every status transition on a ``FireEvent`` — submit, approve, reject, retract —
is recorded as an immutable ``EditorialAction``. The action log is the
authoritative audit trail; the event's current ``EventStatus`` is always
derivable by replaying the log, but is also stored denormalised on the event
row for query convenience.

Transitions allowed by the state machine (methodology/v1.0.pdf §5.1):

  ┌──────────────────────────────────────────────────────────┐
  │  any new event       → PENDING_REVIEW   (submit)         │
  │  PENDING_REVIEW      → PUBLISHED        (approve)         │
  │  PENDING_REVIEW      → REJECTED         (reject)          │
  │  REJECTED            → PENDING_REVIEW   (resubmit)        │
  │  PUBLISHED           → RETRACTED        (retract)         │
  │  PUBLISHED           → PENDING_REVIEW   (anomaly-flag)    │
  └──────────────────────────────────────────────────────────┘

The PUBLISHED → PENDING_REVIEW (anomaly-flag) transition is the
confidence-gated auto-retract from CLAUDE.md §"Confidence-Gated Auto-Publish
Policy" gate #5: the ``anomaly-watch`` agent returns an outlier estimate to
the review queue with a public "under review" note. It is distinct from
``retract`` (PUBLISHED → RETRACTED, terminal): an anomaly-flagged event can be
re-approved after an editor resolves the discrepancy.

Prohibited (raise EditorialTransitionError):
  - Approving a REJECTED event directly (must resubmit first).
  - Retracting without a reason.
  - Any transition out of RETRACTED.
  - Approving or rejecting an already-PUBLISHED event.

CLAUDE.md §"Editorial Workflow" and §"Confidence-Gated Auto-Publish Policy"
govern the policy. The v2 policy allows auto-publishing Confirmed/Verified
events once the code-level publish gate is merged.
"""
from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from wced.models.event import EventStatus


class EditorialActionType(str, enum.Enum):
    """The type of editorial action recorded in the log."""

    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RESUBMITTED = "RESUBMITTED"
    RETRACTED = "RETRACTED"
    ANOMALY_FLAGGED = "ANOMALY_FLAGGED"


class EditorialAction(BaseModel):
    """An immutable record of one editorial decision on a FireEvent.

    EditorialAction rows are append-only: they are never updated or deleted.
    Retractions surface as new rows (action_type=RETRACTED) with a reason
    stored in ``notes`` — the prior PUBLISHED row remains in the log verbatim
    so the full audit trail is always visible.

    Parameters
    ----------
    id : UUID
        Stable identifier for this action row.
    event_id : UUID
        ID of the FireEvent this action applies to.
    action_type : EditorialActionType
        What was done (submitted, approved, rejected, resubmitted, retracted).
    reviewer : str
        Identity of the reviewer or system component that triggered this
        action (human: "analyst:jdoe"; system: "wced.verify.editorial").
    notes : str or None
        Mandatory for REJECTED and RETRACTED (the reason), optional for
        APPROVED and SUBMITTED. Enforced at the queue level, not here, so
        the model can be used for historical rows that pre-date the constraint.
    previous_status : EventStatus
        The event's status immediately before this action was applied.
        Stored denormalised so a reader can reconstruct the full transition
        history without joining against the event table.
    new_status : EventStatus
        The event's status after this action was applied.
    acted_at : AwareDatetime
        Wall-clock time when this action was recorded (always UTC).
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    action_type: EditorialActionType
    reviewer: str = Field(min_length=1)
    notes: str | None = None
    previous_status: EventStatus
    new_status: EventStatus
    acted_at: AwareDatetime


# ---------------------------------------------------------------------------
# State machine helpers
# ---------------------------------------------------------------------------

# Valid (from_status, action_type) → to_status transitions.
_TRANSITIONS: dict[tuple[EventStatus, EditorialActionType], EventStatus] = {
    (EventStatus.PENDING_REVIEW, EditorialActionType.APPROVED): EventStatus.PUBLISHED,
    (EventStatus.PENDING_REVIEW, EditorialActionType.REJECTED): EventStatus.REJECTED,
    (EventStatus.PENDING_REVIEW, EditorialActionType.SUBMITTED): EventStatus.PENDING_REVIEW,
    (EventStatus.REJECTED, EditorialActionType.RESUBMITTED): EventStatus.PENDING_REVIEW,
    (EventStatus.PUBLISHED, EditorialActionType.RETRACTED): EventStatus.RETRACTED,
    (EventStatus.PUBLISHED, EditorialActionType.ANOMALY_FLAGGED): EventStatus.PENDING_REVIEW,
}


class EditorialTransitionError(ValueError):
    """Raised when a requested editorial transition is not permitted.

    Preserves ``event_id``, ``current_status``, and ``action`` as structured
    attributes so callers (the CLI, tests) can surface them without parsing
    the message string.
    """

    def __init__(
        self,
        message: str,
        *,
        event_id: UUID,
        current_status: EventStatus,
        action: EditorialActionType,
    ) -> None:
        super().__init__(message)
        self.event_id = event_id
        self.current_status = current_status
        self.action = action


def validate_transition(
    event_id: UUID,
    current_status: EventStatus,
    action: EditorialActionType,
) -> EventStatus:
    """Return the next status or raise ``EditorialTransitionError``.

    Does NOT enforce note/reason requirements — those are enforced by the
    queue methods that call this function, keeping policy in one place.

    Parameters
    ----------
    event_id : UUID
        The event being transitioned (used in error messages only).
    current_status : EventStatus
        The event's status right now.
    action : EditorialActionType
        The action being requested.

    Returns
    -------
    EventStatus
        The status the event will move to.

    Raises
    ------
    EditorialTransitionError
        If the (current_status, action) pair is not in the allowed table.
    """
    next_status = _TRANSITIONS.get((current_status, action))
    if next_status is None:
        if current_status is EventStatus.RETRACTED:
            msg = f"Event {event_id} is RETRACTED and cannot be transitioned further."
        elif current_status is EventStatus.REJECTED and action is EditorialActionType.APPROVED:
            msg = (
                f"Event {event_id} is REJECTED; call resubmit() first to return it "
                "to PENDING_REVIEW before approving."
            )
        elif current_status is EventStatus.PUBLISHED and action in (
            EditorialActionType.APPROVED,
            EditorialActionType.REJECTED,
        ):
            msg = (
                f"Event {event_id} is already PUBLISHED; "
                "only retract() is valid from this state."
            )
        else:
            msg = (
                f"Transition {action.value} is not permitted "
                f"for event {event_id} in status {current_status.value}."
            )
        raise EditorialTransitionError(
            msg,
            event_id=event_id,
            current_status=current_status,
            action=action,
        )
    return next_status
