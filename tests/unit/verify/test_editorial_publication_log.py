"""Tests that InMemoryReviewQueue appends to a publication_log on every transition.

This verifies gap 0.2: every state-mutating editorial action writes an
append-only record to the publication log, not just to the editorial_actions
internal log.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.verify.editorial import InMemoryReviewQueue


def _event(
    status: EventStatus = EventStatus.PENDING_REVIEW,
) -> FireEvent:
    now = datetime.now(tz=UTC)
    return FireEvent(
        facility_id=uuid4(),
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


class TestPublicationLogOnTransitions:
    def test_approve_appends_to_publication_log(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event()
        queue.submit(event)
        queue.approve(event.id, reviewer="jdoe")
        log = queue.publication_log
        approve_entries = [e for e in log if e["action"] == "approve"]
        assert len(approve_entries) == 1
        entry = approve_entries[0]
        assert entry["target_id"] == event.id
        assert entry["from_state"] == "PENDING_REVIEW"
        assert entry["to_state"] == "PUBLISHED"
        assert entry["actor"] == "jdoe"

    def test_reject_appends_to_publication_log(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event()
        queue.submit(event)
        queue.reject(event.id, reviewer="jdoe", reason="Bad data")
        log = queue.publication_log
        reject_entries = [e for e in log if e["action"] == "reject"]
        assert len(reject_entries) == 1
        assert reject_entries[0]["reason"] == "Bad data"

    def test_retract_appends_to_publication_log(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event()
        queue.submit(event)
        queue.approve(event.id, reviewer="r1")
        queue.retract(event.id, reviewer="r2", reason="Satellite misread")
        log = queue.publication_log
        retract_entries = [e for e in log if e["action"] == "retract"]
        assert len(retract_entries) == 1
        assert retract_entries[0]["reason"] == "Satellite misread"
        assert retract_entries[0]["from_state"] == "PUBLISHED"
        assert retract_entries[0]["to_state"] == "RETRACTED"

    def test_resubmit_appends_to_publication_log(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event()
        queue.submit(event)
        queue.reject(event.id, reviewer="r1", reason="needs more data")
        queue.resubmit(event.id, reviewer="r2", notes="Added GDELT source")
        log = queue.publication_log
        resubmit_entries = [e for e in log if e["action"] == "resubmit"]
        assert len(resubmit_entries) == 1

    def test_full_lifecycle_produces_correct_log_count(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event()
        queue.submit(event)
        # submit does not log to publication_log (it's not a state change)
        queue.reject(event.id, reviewer="r1", reason="initial")
        queue.resubmit(event.id, reviewer="r2")
        queue.approve(event.id, reviewer="r3")
        queue.retract(event.id, reviewer="r4", reason="correction")
        # 4 state-changing transitions
        assert len(queue.publication_log) == 4

    def test_publication_log_entries_are_immutable_dicts(self) -> None:
        queue = InMemoryReviewQueue()
        event = _event()
        queue.submit(event)
        queue.approve(event.id, reviewer="r")
        log = queue.publication_log
        log.clear()
        assert len(queue.publication_log) == 1
