"""Tests for the anomaly-watch agent (Gap 1.4, CLAUDE.md gate #5).

An ``anomaly-watch`` process compares each newly published estimate against:
  1. the facility's history of published estimates, and
  2. the event's cross-method (FRP vs inventory) estimate.

Outliers beyond threshold auto-retract the event to PENDING_REVIEW, append an
``anomaly_retract`` record to the publication_log, and set a public
"under review" note. Thresholds are tuned conservatively so ordinary
variation does NOT trigger a retraction.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pytest

from wced.models.event import DetectionSource, EventStatus, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.quantify.distribution import Distribution
from wced.verify.anomaly_watch import (
    AnomalyAssessment,
    AnomalyThresholds,
    AnomalyWatch,
    evaluate_published_estimate,
)
from wced.verify.editorial import InMemoryReviewQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _published_event(facility_id=None) -> FireEvent:
    now = _now()
    return FireEvent(
        facility_id=facility_id or uuid4(),
        detected_at=now,
        last_seen_at=now,
        peak_frp_mw=75.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.CONFIRMED,
        status=EventStatus.PUBLISHED,
        provenance_id=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _dist(p50: float) -> Distribution:
    """A tight Distribution centred on *p50* (deterministic percentile)."""
    rng = np.random.default_rng(0)
    return Distribution.from_samples(
        rng.normal(p50, abs(p50) * 0.001 + 1e-6, 10_000),
        units="tCO2e",
        methodology_version="1.1.0",
        provenance_id=uuid4(),
    )


def _published_in_queue(queue: InMemoryReviewQueue, event: FireEvent) -> FireEvent:
    """Submit + approve an event so it is genuinely PUBLISHED in the queue."""
    pending = event.model_copy(update={"status": EventStatus.PENDING_REVIEW})
    queue.submit(pending, reviewer="pipeline")
    return queue.approve(pending.id, reviewer="publish_gate:auto")


# ---------------------------------------------------------------------------
# Pure evaluation function
# ---------------------------------------------------------------------------


class TestEvaluatePublishedEstimate:
    def test_normal_value_is_not_anomaly(self) -> None:
        history = [100.0, 110.0, 95.0, 105.0, 102.0]
        a = evaluate_published_estimate(103.0, history, cross_method_p50=101.0)
        assert a.is_anomaly is False
        assert a.kinds == ()

    def test_historical_outlier_flags(self) -> None:
        history = [100.0, 110.0, 95.0, 105.0, 102.0]
        a = evaluate_published_estimate(10_000.0, history)
        assert a.is_anomaly is True
        assert "historical" in a.kinds

    def test_low_historical_outlier_flags(self) -> None:
        history = [100.0, 110.0, 95.0, 105.0, 102.0]
        a = evaluate_published_estimate(0.5, history)
        assert a.is_anomaly is True
        assert "historical" in a.kinds

    def test_cross_method_outlier_flags(self) -> None:
        history = [100.0, 110.0, 95.0, 105.0, 102.0]
        # New estimate fits history, but the cross-method estimate is 5x away.
        a = evaluate_published_estimate(100.0, history, cross_method_p50=500.0)
        assert a.is_anomaly is True
        assert "cross_method" in a.kinds

    def test_insufficient_history_skips_historical_check(self) -> None:
        # Only two prior points — too few to establish a robust baseline.
        a = evaluate_published_estimate(10_000.0, [100.0, 105.0])
        assert "historical" not in a.kinds

    def test_moderate_deviation_does_not_flag(self) -> None:
        # 1.5x the median should be within tolerance (avoid false retraction).
        history = [100.0, 110.0, 95.0, 105.0, 102.0]
        a = evaluate_published_estimate(155.0, history, cross_method_p50=150.0)
        assert a.is_anomaly is False

    def test_zero_mad_uses_ratio_band(self) -> None:
        # All identical history → MAD=0; fall back to a multiplicative band.
        history = [100.0, 100.0, 100.0, 100.0]
        within = evaluate_published_estimate(150.0, history)
        beyond = evaluate_published_estimate(900.0, history)
        assert within.is_anomaly is False
        assert beyond.is_anomaly is True

    def test_no_cross_method_only_checks_history(self) -> None:
        history = [100.0, 110.0, 95.0, 105.0, 102.0]
        a = evaluate_published_estimate(101.0, history, cross_method_p50=None)
        assert a.is_anomaly is False
        assert a.cross_method_ratio is None

    def test_custom_thresholds_respected(self) -> None:
        # A stricter magnitude band turns a 1.5x deviation into an anomaly that
        # the conservative defaults would have let stand.
        history = [100.0, 110.0, 95.0, 105.0, 102.0]
        lenient = evaluate_published_estimate(155.0, history)
        strict = AnomalyThresholds(history_ratio_band=1.4)
        flagged = evaluate_published_estimate(155.0, history, thresholds=strict)
        assert lenient.is_anomaly is False
        assert flagged.is_anomaly is True
        assert "historical" in flagged.kinds


# ---------------------------------------------------------------------------
# AnomalyWatch orchestration (acts on the review queue)
# ---------------------------------------------------------------------------


class TestAnomalyWatchOrchestration:
    def test_normal_estimate_stays_published(self) -> None:
        queue = InMemoryReviewQueue()
        event = _published_in_queue(queue, _published_event())
        watch = AnomalyWatch(queue)

        assessment = watch.review(
            event,
            _dist(103.0),
            facility_history=[100.0, 110.0, 95.0, 105.0, 102.0],
            cross_method_estimate=_dist(101.0),
        )

        assert assessment.is_anomaly is False
        assert queue.get(event.id).status is EventStatus.PUBLISHED
        assert not [e for e in queue.publication_log if e["action"] == "anomaly_retract"]

    def test_historical_outlier_auto_retracts_to_pending(self) -> None:
        queue = InMemoryReviewQueue()
        event = _published_in_queue(queue, _published_event())
        watch = AnomalyWatch(queue)

        assessment = watch.review(
            event,
            _dist(10_000.0),
            facility_history=[100.0, 110.0, 95.0, 105.0, 102.0],
        )

        assert assessment.is_anomaly is True
        assert queue.get(event.id).status is EventStatus.PENDING_REVIEW
        entries = [e for e in queue.publication_log if e["action"] == "anomaly_retract"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["from_state"] == "PUBLISHED"
        assert entry["to_state"] == "PENDING_REVIEW"
        assert entry["public_note"] == "under review"
        assert entry["reason"]  # non-empty explanation

    def test_cross_method_outlier_auto_retracts(self) -> None:
        queue = InMemoryReviewQueue()
        event = _published_in_queue(queue, _published_event())
        watch = AnomalyWatch(queue)

        assessment = watch.review(
            event,
            _dist(100.0),
            facility_history=[100.0, 110.0, 95.0, 105.0, 102.0],
            cross_method_estimate=_dist(500.0),
        )

        assert assessment.is_anomaly is True
        assert "cross_method" in assessment.kinds
        assert queue.get(event.id).status is EventStatus.PENDING_REVIEW

    def test_history_excludes_current_event_and_no_false_retract(self) -> None:
        # A facility's very first published estimate has no prior history.
        queue = InMemoryReviewQueue()
        event = _published_in_queue(queue, _published_event())
        watch = AnomalyWatch(queue)

        assessment = watch.review(
            event,
            _dist(5_000.0),
            facility_history=[],  # first estimate for this facility
        )

        assert assessment.is_anomaly is False
        assert queue.get(event.id).status is EventStatus.PUBLISHED

    def test_idempotent_when_already_pending(self) -> None:
        # If the event is not PUBLISHED, the watch records the assessment but
        # does not attempt an (invalid) state transition.
        queue = InMemoryReviewQueue()
        pending = _published_event().model_copy(
            update={"status": EventStatus.PENDING_REVIEW}
        )
        queue.submit(pending, reviewer="pipeline")
        watch = AnomalyWatch(queue)

        assessment = watch.review(
            pending,
            _dist(10_000.0),
            facility_history=[100.0, 110.0, 95.0, 105.0, 102.0],
        )

        assert assessment.is_anomaly is True
        assert queue.get(pending.id).status is EventStatus.PENDING_REVIEW
        assert not [e for e in queue.publication_log if e["action"] == "anomaly_retract"]


# ---------------------------------------------------------------------------
# Queue-level flag_anomaly transition
# ---------------------------------------------------------------------------


class TestFlagAnomalyTransition:
    def test_flag_anomaly_moves_published_to_pending(self) -> None:
        queue = InMemoryReviewQueue()
        event = _published_in_queue(queue, _published_event())

        updated = queue.flag_anomaly(
            event.id, reviewer="anomaly-watch", reason="3000% above facility median",
        )

        assert updated.status is EventStatus.PENDING_REVIEW
        history = queue.history(event.id)
        assert history[-1].action_type.value == "ANOMALY_FLAGGED"

    def test_flag_anomaly_requires_reason(self) -> None:
        queue = InMemoryReviewQueue()
        event = _published_in_queue(queue, _published_event())
        with pytest.raises(ValueError, match="reason"):
            queue.flag_anomaly(event.id, reviewer="anomaly-watch", reason="")

    def test_flag_anomaly_rejects_non_published(self) -> None:
        from wced.models.editorial import EditorialTransitionError

        queue = InMemoryReviewQueue()
        pending = _published_event().model_copy(
            update={"status": EventStatus.PENDING_REVIEW}
        )
        queue.submit(pending, reviewer="pipeline")
        with pytest.raises(EditorialTransitionError):
            queue.flag_anomaly(pending.id, reviewer="anomaly-watch", reason="x")
