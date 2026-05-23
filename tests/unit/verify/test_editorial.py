"""Tests for wced.verify.editorial and wced.models.editorial.

Covers the full state machine: every permitted transition, every prohibited
one, note/reason requirements, and history tracking.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from wced.models.editorial import (
    EditorialActionType,
    EditorialTransitionError,
    validate_transition,
)
from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.verify.editorial import InMemoryReviewQueue


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _event(
    status: EventStatus = EventStatus.PENDING_REVIEW,
    facility_id: UUID | None = None,
) -> FireEvent:
    now = datetime.now(tz=UTC)
    return FireEvent(
        facility_id=facility_id or uuid4(),
        detected_at=now,
        last_seen_at=now,
        peak_frp_mw=75.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.REPORTED,
        status=status,
        provenance_id=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _submitted_queue(n: int = 1) -> tuple[InMemoryReviewQueue, list[FireEvent]]:
    """Return a queue with *n* events already submitted."""
    queue = InMemoryReviewQueue()
    events = [_event() for _ in range(n)]
    for ev in events:
        queue.submit(ev)
    return queue, events


# ---------------------------------------------------------------------------
# validate_transition — pure state-machine logic
# ---------------------------------------------------------------------------


class TestValidateTransition:
    def test_pending_to_published_via_approve(self) -> None:
        result = validate_transition(
            uuid4(), EventStatus.PENDING_REVIEW, EditorialActionType.APPROVED
        )
        assert result is EventStatus.PUBLISHED

    def test_pending_to_rejected(self) -> None:
        result = validate_transition(
            uuid4(), EventStatus.PENDING_REVIEW, EditorialActionType.REJECTED
        )
        assert result is EventStatus.REJECTED

    def test_rejected_to_pending_via_resubmit(self) -> None:
        result = validate_transition(
            uuid4(), EventStatus.REJECTED, EditorialActionType.RESUBMITTED
        )
        assert result is EventStatus.PENDING_REVIEW

    def test_published_to_retracted(self) -> None:
        result = validate_transition(
            uuid4(), EventStatus.PUBLISHED, EditorialActionType.RETRACTED
        )
        assert result is EventStatus.RETRACTED

    def test_retracted_raises_for_any_action(self) -> None:
        eid = uuid4()
        for action in EditorialActionType:
            if action is EditorialActionType.RETRACTED:
                continue
            with pytest.raises(EditorialTransitionError) as exc_info:
                validate_transition(eid, EventStatus.RETRACTED, action)
            assert exc_info.value.event_id == eid
            assert exc_info.value.current_status is EventStatus.RETRACTED

    def test_rejected_approve_raises_with_helpful_message(self) -> None:
        eid = uuid4()
        with pytest.raises(EditorialTransitionError, match="resubmit"):
            validate_transition(eid, EventStatus.REJECTED, EditorialActionType.APPROVED)

    def test_published_approve_raises(self) -> None:
        with pytest.raises(EditorialTransitionError, match="PUBLISHED"):
            validate_transition(
                uuid4(), EventStatus.PUBLISHED, EditorialActionType.APPROVED
            )

    def test_published_reject_raises(self) -> None:
        with pytest.raises(EditorialTransitionError):
            validate_transition(
                uuid4(), EventStatus.PUBLISHED, EditorialActionType.REJECTED
            )


# ---------------------------------------------------------------------------
# InMemoryReviewQueue — submit
# ---------------------------------------------------------------------------


class TestSubmit:
    def test_new_pending_event_accepted(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event(EventStatus.PENDING_REVIEW)
        returned = queue.submit(event)
        assert returned.id == event.id
        assert len(queue) == 1

    def test_submit_records_submitted_action(self) -> None:
        queue, [event] = _submitted_queue()
        history = queue.history(event.id)
        assert len(history) == 1
        assert history[0].action_type is EditorialActionType.SUBMITTED
        assert history[0].previous_status is EventStatus.PENDING_REVIEW
        assert history[0].new_status is EventStatus.PENDING_REVIEW

    def test_idempotent_when_already_pending(self) -> None:
        queue, [event] = _submitted_queue()
        returned = queue.submit(event)  # second submit
        assert returned.id == event.id
        assert len(queue) == 1
        # Only one action written — the second submit is a no-op.
        assert len(queue.history(event.id)) == 1

    def test_raises_if_event_not_pending_review(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event(EventStatus.PUBLISHED)
        with pytest.raises(ValueError, match="PENDING_REVIEW"):
            queue.submit(event)

    def test_raises_if_known_event_in_different_status(self) -> None:
        queue, [event] = _submitted_queue()
        approved = queue.approve(event.id, reviewer="jdoe")
        with pytest.raises(ValueError, match="already in the queue"):
            queue.submit(approved)


# ---------------------------------------------------------------------------
# pending()
# ---------------------------------------------------------------------------


class TestPending:
    def test_returns_only_pending_events(self) -> None:
        queue, [ev1, ev2, ev3] = _submitted_queue(3)
        queue.approve(ev1.id, reviewer="r")
        queue.reject(ev2.id, reviewer="r", reason="bad data")
        pending = queue.pending()
        assert len(pending) == 1
        assert pending[0].id == ev3.id

    def test_sorted_by_created_at_ascending(self) -> None:
        queue, events = _submitted_queue(3)
        pending = queue.pending()
        created_ats = [e.created_at for e in pending]
        assert created_ats == sorted(created_ats)

    def test_empty_queue_returns_empty_list(self) -> None:
        queue = InMemoryReviewQueue()
        assert queue.pending() == []


# ---------------------------------------------------------------------------
# approve()
# ---------------------------------------------------------------------------


class TestApprove:
    def test_pending_to_published(self) -> None:
        queue, [event] = _submitted_queue()
        updated = queue.approve(event.id, reviewer="jdoe", notes="All checks passed.")
        assert updated.status is EventStatus.PUBLISHED

    def test_approve_writes_action(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="jdoe", notes="ok")
        actions = queue.history(event.id)
        approved_actions = [a for a in actions if a.action_type is EditorialActionType.APPROVED]
        assert len(approved_actions) == 1
        assert approved_actions[0].notes == "ok"
        assert approved_actions[0].reviewer == "jdoe"

    def test_approve_rejected_event_raises(self) -> None:
        queue, [event] = _submitted_queue()
        queue.reject(event.id, reviewer="r", reason="wrong data")
        with pytest.raises(EditorialTransitionError, match="resubmit"):
            queue.approve(event.id, reviewer="jdoe")

    def test_approve_already_published_raises(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        with pytest.raises(EditorialTransitionError):
            queue.approve(event.id, reviewer="r2")

    def test_approve_unknown_event_raises_key_error(self) -> None:
        queue = InMemoryReviewQueue()
        with pytest.raises(KeyError):
            queue.approve(uuid4(), reviewer="r")


# ---------------------------------------------------------------------------
# reject()
# ---------------------------------------------------------------------------


class TestReject:
    def test_pending_to_rejected(self) -> None:
        queue, [event] = _submitted_queue()
        updated = queue.reject(event.id, reviewer="jdoe", reason="Sensor glitch.")
        assert updated.status is EventStatus.REJECTED

    def test_reject_requires_non_empty_reason(self) -> None:
        queue, [event] = _submitted_queue()
        with pytest.raises(ValueError, match="non-empty reason"):
            queue.reject(event.id, reviewer="jdoe", reason="")

    def test_reject_whitespace_reason_raises(self) -> None:
        queue, [event] = _submitted_queue()
        with pytest.raises(ValueError):
            queue.reject(event.id, reviewer="jdoe", reason="   ")

    def test_reject_writes_reason_in_action(self) -> None:
        queue, [event] = _submitted_queue()
        queue.reject(event.id, reviewer="jdoe", reason="False positive — routine flaring.")
        actions = [a for a in queue.history(event.id) if a.action_type is EditorialActionType.REJECTED]
        assert actions[0].notes == "False positive — routine flaring."

    def test_reject_published_event_raises(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        with pytest.raises(EditorialTransitionError):
            queue.reject(event.id, reviewer="r2", reason="late finding")


# ---------------------------------------------------------------------------
# resubmit()
# ---------------------------------------------------------------------------


class TestResubmit:
    def test_rejected_returns_to_pending(self) -> None:
        queue, [event] = _submitted_queue()
        queue.reject(event.id, reviewer="r", reason="needs context")
        updated = queue.resubmit(event.id, reviewer="analyst:jdoe", notes="Added ACLED source.")
        assert updated.status is EventStatus.PENDING_REVIEW

    def test_resubmitted_event_appears_in_pending(self) -> None:
        queue, [event] = _submitted_queue()
        queue.reject(event.id, reviewer="r", reason="bad data")
        queue.resubmit(event.id, reviewer="jdoe")
        assert any(e.id == event.id for e in queue.pending())

    def test_cannot_resubmit_pending_event(self) -> None:
        queue, [event] = _submitted_queue()
        with pytest.raises(EditorialTransitionError):
            queue.resubmit(event.id, reviewer="jdoe")

    def test_can_approve_after_resubmit(self) -> None:
        queue, [event] = _submitted_queue()
        queue.reject(event.id, reviewer="r", reason="initial rejection")
        queue.resubmit(event.id, reviewer="r2")
        updated = queue.approve(event.id, reviewer="r3")
        assert updated.status is EventStatus.PUBLISHED

    def test_full_history_recorded(self) -> None:
        queue, [event] = _submitted_queue()
        queue.reject(event.id, reviewer="r", reason="bad")
        queue.resubmit(event.id, reviewer="r2")
        queue.approve(event.id, reviewer="r3")
        action_types = [a.action_type for a in queue.history(event.id)]
        assert action_types == [
            EditorialActionType.SUBMITTED,
            EditorialActionType.REJECTED,
            EditorialActionType.RESUBMITTED,
            EditorialActionType.APPROVED,
        ]


# ---------------------------------------------------------------------------
# retract()
# ---------------------------------------------------------------------------


class TestRetract:
    def test_published_to_retracted(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        updated = queue.retract(
            event.id,
            reviewer="editor:jdoe",
            reason="Satellite imagery was misread — strike was a near-miss.",
        )
        assert updated.status is EventStatus.RETRACTED

    def test_retract_requires_non_empty_reason(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        with pytest.raises(ValueError, match="non-empty reason"):
            queue.retract(event.id, reviewer="r2", reason="")

    def test_retract_reason_stored_in_action(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        queue.retract(event.id, reviewer="r2", reason="New evidence contradicts detection.")
        actions = [
            a for a in queue.history(event.id)
            if a.action_type is EditorialActionType.RETRACTED
        ]
        assert actions[0].notes == "New evidence contradicts detection."

    def test_retracted_event_not_in_pending(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        queue.retract(event.id, reviewer="r2", reason="Sensor glitch confirmed.")
        assert not any(e.id == event.id for e in queue.pending())

    def test_cannot_retract_pending_event(self) -> None:
        queue, [event] = _submitted_queue()
        with pytest.raises(EditorialTransitionError):
            queue.retract(event.id, reviewer="r", reason="premature")

    def test_cannot_retract_rejected_event(self) -> None:
        queue, [event] = _submitted_queue()
        queue.reject(event.id, reviewer="r", reason="bad data")
        with pytest.raises(EditorialTransitionError):
            queue.retract(event.id, reviewer="r2", reason="double reject")

    def test_retracted_is_terminal_cannot_approve(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        queue.retract(event.id, reviewer="r2", reason="retract for test")
        with pytest.raises(EditorialTransitionError):
            queue.approve(event.id, reviewer="r3")

    def test_retracted_is_terminal_cannot_reject(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        queue.retract(event.id, reviewer="r2", reason="retract for test")
        with pytest.raises(EditorialTransitionError):
            queue.reject(event.id, reviewer="r3", reason="too late")

    def test_retracted_is_terminal_cannot_resubmit(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        queue.retract(event.id, reviewer="r2", reason="retract for test")
        with pytest.raises(EditorialTransitionError):
            queue.resubmit(event.id, reviewer="r3")


# ---------------------------------------------------------------------------
# get() and history()
# ---------------------------------------------------------------------------


class TestGetAndHistory:
    def test_get_returns_current_state(self) -> None:
        queue, [event] = _submitted_queue()
        queue.approve(event.id, reviewer="r")
        fetched = queue.get(event.id)
        assert fetched.status is EventStatus.PUBLISHED

    def test_get_unknown_raises_key_error(self) -> None:
        queue = InMemoryReviewQueue()
        with pytest.raises(KeyError):
            queue.get(uuid4())

    def test_history_unknown_raises_key_error(self) -> None:
        queue = InMemoryReviewQueue()
        with pytest.raises(KeyError):
            queue.history(uuid4())

    def test_history_is_immutable_copy(self) -> None:
        queue, [event] = _submitted_queue()
        h1 = queue.history(event.id)
        h1.append(None)  # type: ignore[arg-type]
        h2 = queue.history(event.id)
        assert len(h2) == 1  # original not mutated


# ---------------------------------------------------------------------------
# No-auto-publish guard (policy check)
# ---------------------------------------------------------------------------


class TestNoAutoPublish:
    """Confirm there is no path that bypasses manual review."""

    def test_submit_does_not_publish(self) -> None:
        queue, [event] = _submitted_queue()
        stored = queue.get(event.id)
        assert stored.status is EventStatus.PENDING_REVIEW

    def test_confirmed_confidence_does_not_auto_approve(self) -> None:
        queue = InMemoryReviewQueue()
        now = datetime.now(tz=UTC)
        high_conf_event = FireEvent(
            facility_id=uuid4(),
            detected_at=now,
            last_seen_at=now,
            peak_frp_mw=200.0,
            detection_source=DetectionSource.FIRMS_VIIRS,
            confidence_label=ConfidenceLabel.CONFIRMED,  # highest confidence
            status=EventStatus.PENDING_REVIEW,
            provenance_id=uuid4(),
            created_at=now,
            updated_at=now,
        )
        queue.submit(high_conf_event)
        stored = queue.get(high_conf_event.id)
        # Still pending — CONFIRMED confidence does not auto-publish.
        assert stored.status is EventStatus.PENDING_REVIEW
