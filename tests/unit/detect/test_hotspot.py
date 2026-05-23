"""Tests for wced.detect.hotspot.

Covers:
- _haversine_distances_m: single-point identity, known-distance pairs
- _dbscan_labels: trivial cases, cluster merging, spatial separation
- _build_candidate: field derivations
- hotspots_to_candidates: empty input, single hotspot, temporal split,
  spatial split
- Property test (Hypothesis): hotspots from two non-overlapping spatial
  clusters are always attributed to separate candidates.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wced.detect.hotspot import (
    CandidateFireEvent,
    FIRMSDetection,
    _build_candidate,
    _dbscan_labels,
    _haversine_distances_m,
    hotspots_to_candidates,
)
from wced.models.event import DetectionSource
from wced.provenance.store import InMemoryProvenanceStore


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 3, 15, 6, 0, tzinfo=UTC)
_SOURCE_ID = uuid4()


def make_detection(
    lat: float = 32.0,
    lon: float = 51.0,
    frp_mw: float = 20.0,
    detected_at: datetime = _T0,
    source_id: None | object = None,
) -> FIRMSDetection:
    return FIRMSDetection(
        latitude=lat,
        longitude=lon,
        frp_mw=frp_mw,
        detected_at=detected_at,
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=320.0,
        confidence="n",
        source_id=source_id or _SOURCE_ID,
    )


# ---------------------------------------------------------------------------
# _haversine_distances_m
# ---------------------------------------------------------------------------


class TestHaversineDistances:
    def test_zero_distance_on_diagonal(self) -> None:
        lats = np.array([32.0, 33.0])
        lons = np.array([51.0, 52.0])
        d = _haversine_distances_m(lats, lons)
        assert d[0, 0] == pytest.approx(0.0, abs=1e-6)
        assert d[1, 1] == pytest.approx(0.0, abs=1e-6)

    def test_symmetry(self) -> None:
        lats = np.array([32.0, 33.0, 34.0])
        lons = np.array([51.0, 51.5, 52.0])
        d = _haversine_distances_m(lats, lons)
        np.testing.assert_allclose(d, d.T, atol=1e-6)

    def test_known_distance_approximately_correct(self) -> None:
        # One degree of latitude at mid-latitudes ≈ 111 km.
        lats = np.array([32.0, 33.0])
        lons = np.array([51.0, 51.0])
        d = _haversine_distances_m(lats, lons)
        assert 110_000 < d[0, 1] < 112_000

    def test_single_point_returns_1x1_zero(self) -> None:
        d = _haversine_distances_m(np.array([32.0]), np.array([51.0]))
        assert d.shape == (1, 1)
        assert d[0, 0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _dbscan_labels
# ---------------------------------------------------------------------------


class TestDbscanLabels:
    def test_single_point_forms_cluster_0(self) -> None:
        dist = np.array([[0.0]])
        assert _dbscan_labels(dist, 500.0) == [0]

    def test_two_close_points_same_cluster(self) -> None:
        # 300 m apart — below 500 m eps
        dist = np.array([[0.0, 300.0], [300.0, 0.0]])
        labels = _dbscan_labels(dist, 500.0)
        assert labels[0] == labels[1]

    def test_two_far_points_different_clusters(self) -> None:
        # 600 m apart — above 500 m eps
        dist = np.array([[0.0, 600.0], [600.0, 0.0]])
        labels = _dbscan_labels(dist, 500.0)
        assert labels[0] != labels[1]

    def test_three_points_chain_merges_via_middle(self) -> None:
        # A–B: 400 m, B–C: 400 m, A–C: 800 m
        # A and C should still be in the same cluster through B.
        dist = np.array([
            [0.0, 400.0, 800.0],
            [400.0, 0.0, 400.0],
            [800.0, 400.0, 0.0],
        ])
        labels = _dbscan_labels(dist, 500.0)
        assert labels[0] == labels[1] == labels[2]

    def test_no_noise_labels(self) -> None:
        lats = np.array([32.0, 33.0, 34.0])
        lons = np.array([51.0, 55.0, 59.0])
        dist = _haversine_distances_m(lats, lons)
        labels = _dbscan_labels(dist, 500.0)
        assert -1 not in labels  # min_samples=1 → no noise


# ---------------------------------------------------------------------------
# _build_candidate
# ---------------------------------------------------------------------------


class TestBuildCandidate:
    def test_single_hotspot(self) -> None:
        h = make_detection(lat=32.0, lon=51.0, frp_mw=15.0)
        prov_id = uuid4()
        c = _build_candidate([h], prov_id)
        assert c.peak_frp_mw == pytest.approx(15.0)
        assert c.mean_frp_mw == pytest.approx(15.0)
        assert c.centroid_lat == pytest.approx(32.0)
        assert c.centroid_lon == pytest.approx(51.0)
        assert c.n_overpasses == 1
        assert c.first_detected_at == _T0
        assert c.last_detected_at == _T0

    def test_two_hotspots_same_overpass(self) -> None:
        h1 = make_detection(frp_mw=10.0)
        h2 = make_detection(frp_mw=30.0)
        c = _build_candidate([h1, h2], uuid4())
        assert c.peak_frp_mw == pytest.approx(30.0)
        assert c.n_overpasses == 1  # same detected_at

    def test_two_hotspots_different_overpasses(self) -> None:
        h1 = make_detection(frp_mw=10.0, detected_at=_T0)
        h2 = make_detection(frp_mw=10.0, detected_at=_T0 + timedelta(hours=12))
        c = _build_candidate([h1, h2], uuid4())
        assert c.n_overpasses == 2
        assert c.first_detected_at == _T0
        assert c.last_detected_at == _T0 + timedelta(hours=12)

    def test_raises_on_empty_input(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            _build_candidate([], uuid4())

    def test_hotspots_sorted_by_time(self) -> None:
        h1 = make_detection(detected_at=_T0 + timedelta(hours=12))
        h2 = make_detection(detected_at=_T0)
        c = _build_candidate([h1, h2], uuid4())
        assert c.hotspots[0].detected_at == _T0


# ---------------------------------------------------------------------------
# hotspots_to_candidates
# ---------------------------------------------------------------------------


class TestHotspotsToCanidates:
    def test_empty_returns_empty(self) -> None:
        store = InMemoryProvenanceStore()
        assert hotspots_to_candidates([], store=store) == []

    def test_single_hotspot_one_candidate(self) -> None:
        store = InMemoryProvenanceStore()
        h = make_detection()
        results = hotspots_to_candidates([h], store=store)
        assert len(results) == 1
        assert results[0].n_overpasses == 1

    def test_two_close_hotspots_same_candidate(self) -> None:
        # 50 m apart — well within 500 m eps
        store = InMemoryProvenanceStore()
        h1 = make_detection(lat=32.0000, lon=51.0000)
        h2 = make_detection(lat=32.0004, lon=51.0000)  # ~44 m north
        results = hotspots_to_candidates([h1, h2], store=store)
        assert len(results) == 1
        assert results[0].n_overpasses == 1  # same detected_at

    def test_two_far_hotspots_different_candidates(self) -> None:
        # ~1.1 km apart — beyond 500 m eps
        store = InMemoryProvenanceStore()
        h1 = make_detection(lat=32.0, lon=51.0)
        h2 = make_detection(lat=32.01, lon=51.0)
        results = hotspots_to_candidates([h1, h2], store=store)
        assert len(results) == 2

    def test_temporal_split_on_gap_exceeding_24h(self) -> None:
        # Same location, but 25 h between overpasses → should produce 2 candidates
        store = InMemoryProvenanceStore()
        h1 = make_detection(lat=32.0, lon=51.0, detected_at=_T0)
        h2 = make_detection(lat=32.0001, lon=51.0, detected_at=_T0 + timedelta(hours=25))
        results = hotspots_to_candidates([h1, h2], store=store)
        assert len(results) == 2

    def test_temporal_split_within_24h_stays_merged(self) -> None:
        # Same location, 23 h apart → one candidate
        store = InMemoryProvenanceStore()
        h1 = make_detection(lat=32.0, lon=51.0, detected_at=_T0)
        h2 = make_detection(lat=32.0001, lon=51.0, detected_at=_T0 + timedelta(hours=23))
        results = hotspots_to_candidates([h1, h2], store=store)
        assert len(results) == 1
        assert results[0].n_overpasses == 2

    def test_provenance_emitted_per_candidate(self) -> None:
        store = InMemoryProvenanceStore()
        h1 = make_detection(lat=32.0, lon=51.0)
        h2 = make_detection(lat=32.1, lon=51.0)  # far apart
        hotspots_to_candidates([h1, h2], store=store)
        assert len(store) == 2  # one record per candidate

    def test_candidate_ids_reference_provenance_in_store(self) -> None:
        store = InMemoryProvenanceStore()
        h = make_detection()
        (candidate,) = hotspots_to_candidates([h], store=store)
        # The provenance_id must exist in the store.
        node = store.get(candidate.provenance_id)
        assert node.produced_by == "wced.detect.hotspot"

    def test_custom_eps_m(self) -> None:
        # With eps=100 m, two points 200 m apart should be separate.
        store = InMemoryProvenanceStore()
        h1 = make_detection(lat=32.0, lon=51.0)
        h2 = make_detection(lat=32.002, lon=51.0)  # ~222 m north
        results = hotspots_to_candidates([h1, h2], store=store, eps_m=100.0)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Property test: non-overlapping clusters always split
# ---------------------------------------------------------------------------


@given(
    center_lat=st.floats(min_value=29.0, max_value=37.0),
    center_lon=st.floats(min_value=44.0, max_value=63.0),
    n1=st.integers(min_value=1, max_value=4),
    n2=st.integers(min_value=1, max_value=4),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=80)
def test_non_overlapping_clusters_are_always_split(
    center_lat: float,
    center_lon: float,
    n1: int,
    n2: int,
) -> None:
    """Hotspots from two non-overlapping spatial clusters must end up in
    separate candidates after clustering.

    Cluster centres are separated by 0.05° latitude (≈ 5.5 km >> 500 m eps).
    Individual hotspots within each cluster are offset by at most ±0.001°
    (≈ ±110 m << 500 m) so they remain within the same spatial cluster.
    """
    # Cluster 1 is near (center_lat, center_lon).
    # Cluster 2 is ~5.5 km north to guarantee they are beyond eps=500 m.
    source_id = uuid4()

    def make(lat: float, lon: float, offset_i: int) -> FIRMSDetection:
        return FIRMSDetection(
            latitude=lat + offset_i * 0.0001,
            longitude=lon,
            frp_mw=20.0,
            detected_at=_T0,
            detection_source=DetectionSource.FIRMS_VIIRS,
            brightness_k=325.0,
            confidence="n",
            source_id=source_id,
        )

    cluster_1 = [make(center_lat, center_lon, i) for i in range(n1)]
    cluster_2 = [make(center_lat + 0.05, center_lon, i) for i in range(n2)]

    ids_1 = {h.id for h in cluster_1}
    ids_2 = {h.id for h in cluster_2}

    store = InMemoryProvenanceStore()
    candidates = hotspots_to_candidates(cluster_1 + cluster_2, store=store)

    # There must be at least 2 candidates.
    assert len(candidates) >= 2

    # No candidate may mix hotspots from both clusters.
    for cand in candidates:
        hotspot_ids = {h.id for h in cand.hotspots}
        mixed = hotspot_ids & ids_1 and hotspot_ids & ids_2
        assert not mixed, (
            f"Candidate mixed hotspots from cluster 1 and cluster 2: "
            f"overlap={hotspot_ids & ids_1 & ids_2}"
        )

    # Every hotspot appears in exactly one candidate.
    all_ids_in_candidates = {h.id for cand in candidates for h in cand.hotspots}
    assert all_ids_in_candidates == ids_1 | ids_2
