"""Tests for wced.detect.persistence.

Covers:
- is_persistent_event: persistent (≥2 qualifying overpasses in 24 h),
  single overpass, insufficient FRP, overpasses outside window,
  fallback baseline edge cases
- candidate_status: always returns PENDING_REVIEW
- Provenance emitted with correct confidence labels
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from wced.detect.baseline import FacilityBaseline
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.detect.persistence import (
    FRP_BASELINE_MULTIPLIER,
    MIN_QUALIFYING_OVERPASSES,
    PERSISTENCE_WINDOW_H,
    candidate_status,
    is_persistent_event,
)
from wced.models.event import DetectionSource, EventStatus
from wced.models.provenance import ConfidenceLabel
from wced.provenance.store import InMemoryProvenanceStore

_T0 = datetime(2026, 3, 15, 6, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_baseline(
    frp_mw: float = 5.0,
    std_mw: float = 1.0,
    *,
    is_fallback: bool = False,
) -> FacilityBaseline:
    return FacilityBaseline(
        facility_id=uuid4(),
        baseline_frp_mw=frp_mw,
        baseline_std_mw=std_mw,
        n_observations=0 if is_fallback else 10,
        window_start=_T0 - timedelta(days=30),
        window_end=_T0,
        computed_at=_T0,
        is_fallback=is_fallback,
        provenance_id=uuid4(),
    )


def make_detection(
    frp_mw: float,
    detected_at: datetime,
) -> FIRMSDetection:
    return FIRMSDetection(
        latitude=32.0,
        longitude=51.0,
        frp_mw=frp_mw,
        detected_at=detected_at,
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=325.0,
        confidence="n",
        source_id=uuid4(),
    )


def make_candidate(hotspots: list[FIRMSDetection]) -> CandidateFireEvent:
    ordered = sorted(hotspots, key=lambda h: h.detected_at)
    return CandidateFireEvent(
        hotspots=tuple(ordered),
        centroid_lat=32.0,
        centroid_lon=51.0,
        first_detected_at=ordered[0].detected_at,
        last_detected_at=ordered[-1].detected_at,
        peak_frp_mw=max(h.frp_mw for h in hotspots),
        mean_frp_mw=sum(h.frp_mw for h in hotspots) / len(hotspots),
        n_overpasses=len({h.detected_at for h in hotspots}),
        provenance_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# Persistence criterion
# ---------------------------------------------------------------------------


class TestIsPersistentEvent:
    def test_two_qualifying_overpasses_within_24h(self) -> None:
        # Baseline 5 MW; threshold = 2×5 = 10 MW.
        # Two overpasses at 20 MW, 12 h apart → persistent.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        assert is_persistent_event(c, baseline, store=store) is True

    def test_single_overpass_not_persistent(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h = make_detection(100.0, _T0)
        c = make_candidate([h])
        assert is_persistent_event(c, baseline, store=store) is False

    def test_frp_at_exact_threshold_does_not_qualify(self) -> None:
        # threshold = 2×10 = 20 MW; FRP = 20 MW → must be STRICTLY greater.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=10.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        # 20.0 > 20.0 is False → neither overpass qualifies
        assert is_persistent_event(c, baseline, store=store) is False

    def test_frp_above_threshold_qualifies(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=10.0)
        h1 = make_detection(21.0, _T0)
        h2 = make_detection(21.0, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        assert is_persistent_event(c, baseline, store=store) is True

    def test_overpasses_beyond_24h_window_not_persistent(self) -> None:
        # Two qualifying overpasses 25 h apart — outside the 24 h window.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=25))
        c = make_candidate([h1, h2])
        assert is_persistent_event(c, baseline, store=store) is False

    def test_three_passes_with_gap_uses_sliding_window(self) -> None:
        # 3 qualifying overpasses: 0 h, 20 h, 26 h.
        # Passes 0→20 are within 24 h → should be persistent.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=20))
        h3 = make_detection(20.0, _T0 + timedelta(hours=26))
        c = make_candidate([h1, h2, h3])
        assert is_persistent_event(c, baseline, store=store) is True

    def test_mixed_frp_only_qualifying_overpasses_count(self) -> None:
        # threshold = 2×10 = 20 MW. Pass 1: 5 MW (below); Pass 2 & 3: 25 MW.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=10.0)
        h1 = make_detection(5.0, _T0)                      # below threshold
        h2 = make_detection(25.0, _T0 + timedelta(hours=6))
        h3 = make_detection(25.0, _T0 + timedelta(hours=18))
        c = make_candidate([h1, h2, h3])
        assert is_persistent_event(c, baseline, store=store) is True

    def test_zero_baseline_any_frp_qualifies(self) -> None:
        # Fallback baseline (frp_mw=0) → threshold = 0 → any FRP > 0 qualifies.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=0.0, is_fallback=True)
        h1 = make_detection(0.5, _T0)
        h2 = make_detection(0.5, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        assert is_persistent_event(c, baseline, store=store) is True

    def test_multiple_hotspots_same_overpass_counts_as_one(self) -> None:
        # Two hotspots at the same detected_at should count as one overpass.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(25.0, _T0)  # same timestamp
        c = make_candidate([h1, h2])
        assert is_persistent_event(c, baseline, store=store) is False


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class TestPersistenceProvenance:
    def test_provenance_emitted_on_persistent(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        is_persistent_event(c, baseline, store=store)
        assert len(store) == 1

    def test_confidence_label_reported_when_persistent(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        is_persistent_event(c, baseline, store=store)
        nodes = list(store._store.values())
        assert nodes[0].confidence_label == ConfidenceLabel.REPORTED

    def test_confidence_label_suspected_when_singleton(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h = make_detection(100.0, _T0)
        c = make_candidate([h])
        is_persistent_event(c, baseline, store=store)
        nodes = list(store._store.values())
        assert nodes[0].confidence_label == ConfidenceLabel.SUSPECTED

    def test_provenance_inputs_include_candidate_and_baseline(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        is_persistent_event(c, baseline, store=store)
        node = next(iter(store._store.values()))
        assert c.provenance_id in node.inputs
        assert baseline.provenance_id in node.inputs

    def test_parameters_record_threshold_values(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h = make_detection(20.0, _T0)
        c = make_candidate([h])
        is_persistent_event(c, baseline, store=store)
        params = next(iter(store._store.values())).parameters
        assert params["frp_multiplier"] == FRP_BASELINE_MULTIPLIER
        assert params["min_overpasses"] == MIN_QUALIFYING_OVERPASSES
        assert params["temporal_window_h"] == PERSISTENCE_WINDOW_H


# ---------------------------------------------------------------------------
# candidate_status
# ---------------------------------------------------------------------------


class TestCandidateStatus:
    def test_persistent_candidate_is_pending_review(self) -> None:
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h1 = make_detection(20.0, _T0)
        h2 = make_detection(20.0, _T0 + timedelta(hours=12))
        c = make_candidate([h1, h2])
        status = candidate_status(c, baseline, store=store)
        assert status == EventStatus.PENDING_REVIEW

    def test_singleton_candidate_also_pending_review(self) -> None:
        # Singletons still enter PENDING_REVIEW; editorial decides.
        store = InMemoryProvenanceStore()
        baseline = make_baseline(frp_mw=5.0)
        h = make_detection(100.0, _T0)
        c = make_candidate([h])
        status = candidate_status(c, baseline, store=store)
        assert status == EventStatus.PENDING_REVIEW
