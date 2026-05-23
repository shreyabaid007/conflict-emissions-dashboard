"""Tests for wced.detect.facility_match.

Covers:
- _haversine_m: known distances
- _nearest_point_on_geom: Point and Polygon cases, interior containment
- distance_to_facility_m: Point and Polygon facilities
- match_to_facility: matched, unmatched, empty facility list cases
- match_to_facility_with_tree: equivalence with non-tree variant
- Provenance emitted in all cases
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from wced.detect.facility_match import (
    DEFAULT_THRESHOLD_M,
    _haversine_m,
    _nearest_point_on_geom,
    build_facility_tree,
    distance_to_facility_m,
    match_to_facility,
    match_to_facility_with_tree,
)
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.models.event import DetectionSource
from wced.models.facility import Facility, FacilityType
from wced.models.provenance import ConfidenceLabel
from wced.provenance.store import InMemoryProvenanceStore

from shapely.geometry import Point, Polygon

_T0 = datetime(2026, 3, 15, 6, 0, tzinfo=UTC)
_ADDED_AT = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_facility(
    lat: float = 30.36,
    lon: float = 48.3,
    *,
    as_polygon: bool = False,
    facility_type: FacilityType = FacilityType.REFINERY,
) -> Facility:
    if as_polygon:
        # Small 0.01° × 0.01° square around the centre point
        wkt = (
            f"POLYGON(({lon - 0.005} {lat - 0.005}, "
            f"{lon + 0.005} {lat - 0.005}, "
            f"{lon + 0.005} {lat + 0.005}, "
            f"{lon - 0.005} {lat + 0.005}, "
            f"{lon - 0.005} {lat - 0.005}))"
        )
    else:
        wkt = f"POINT({lon} {lat})"
    return Facility(
        name="Test Facility",
        facility_type=facility_type,
        geometry_wkt=wkt,
        country="IRN",
        source_url="https://example.org",
        added_at=_ADDED_AT,
    )


def make_candidate(
    lat: float = 30.36,
    lon: float = 48.3,
    frp_mw: float = 25.0,
) -> CandidateFireEvent:
    source_id = uuid4()
    hotspot = FIRMSDetection(
        latitude=lat,
        longitude=lon,
        frp_mw=frp_mw,
        detected_at=_T0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=320.0,
        confidence="n",
        source_id=source_id,
    )
    return CandidateFireEvent(
        hotspots=(hotspot,),
        centroid_lat=lat,
        centroid_lon=lon,
        first_detected_at=_T0,
        last_detected_at=_T0,
        peak_frp_mw=frp_mw,
        mean_frp_mw=frp_mw,
        n_overpasses=1,
        provenance_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# _haversine_m
# ---------------------------------------------------------------------------


class TestHaversineM:
    def test_zero_distance_same_point(self) -> None:
        assert _haversine_m(32.0, 51.0, 32.0, 51.0) == pytest.approx(0.0, abs=1e-6)

    def test_one_degree_latitude_approx_111km(self) -> None:
        d = _haversine_m(32.0, 51.0, 33.0, 51.0)
        assert 110_000 < d < 112_000

    def test_symmetry(self) -> None:
        d1 = _haversine_m(32.0, 51.0, 33.0, 52.0)
        d2 = _haversine_m(33.0, 52.0, 32.0, 51.0)
        assert d1 == pytest.approx(d2, rel=1e-9)

    def test_small_offset_approximately_correct(self) -> None:
        # 0.001° latitude ≈ 111 m
        d = _haversine_m(32.0, 51.0, 32.001, 51.0)
        assert 100 < d < 120


# ---------------------------------------------------------------------------
# _nearest_point_on_geom
# ---------------------------------------------------------------------------


class TestNearestPointOnGeom:
    def test_point_geometry_returns_point_coords(self) -> None:
        geom = Point(51.0, 32.0)  # (lon, lat)
        lat, lon = _nearest_point_on_geom(geom, 32.0, 51.0)
        assert lat == pytest.approx(32.0)
        assert lon == pytest.approx(51.0)

    def test_polygon_exterior_when_outside(self) -> None:
        # Polygon centred at (32.0, 51.0) with half-width 0.01°
        poly = Polygon([
            (50.99, 31.99), (51.01, 31.99),
            (51.01, 32.01), (50.99, 32.01),
        ])
        # Query from directly north of the polygon, outside it
        lat, lon = _nearest_point_on_geom(poly, 32.02, 51.0)
        # The nearest point should be on the northern edge at lat ≈ 32.01
        assert lat == pytest.approx(32.01, abs=0.001)

    def test_polygon_returns_query_point_when_inside(self) -> None:
        # A point inside the polygon should yield (itself, itself) from
        # nearest_points → distance 0.
        poly = Polygon([(50.0, 30.0), (52.0, 30.0), (52.0, 34.0), (50.0, 34.0)])
        # Interior query at (32.0, 51.0)
        lat, lon = _nearest_point_on_geom(poly, 32.0, 51.0)
        assert lat == pytest.approx(32.0, abs=0.001)
        assert lon == pytest.approx(51.0, abs=0.001)


# ---------------------------------------------------------------------------
# distance_to_facility_m
# ---------------------------------------------------------------------------


class TestDistanceToFacilityM:
    def test_zero_distance_at_facility_point(self) -> None:
        f = make_facility(lat=30.36, lon=48.3)
        d = distance_to_facility_m(30.36, 48.3, f)
        assert d == pytest.approx(0.0, abs=1.0)

    def test_100m_north_of_point_facility(self) -> None:
        f = make_facility(lat=30.36, lon=48.3)
        # 0.001° latitude ≈ 111 m
        d = distance_to_facility_m(30.361, 48.3, f)
        assert 100 < d < 125

    def test_zero_distance_inside_polygon_facility(self) -> None:
        # The facility is a 0.01° × 0.01° polygon; query is at the centre
        f = make_facility(lat=30.36, lon=48.3, as_polygon=True)
        d = distance_to_facility_m(30.36, 48.3, f)
        assert d == pytest.approx(0.0, abs=1.0)

    def test_outside_polygon_facility(self) -> None:
        # Polygon is ±0.005° around (30.36, 48.3); query is 0.02° north
        f = make_facility(lat=30.36, lon=48.3, as_polygon=True)
        d = distance_to_facility_m(30.38, 48.3, f)
        # Distance should be about 0.015° × 111 000 m/° ≈ 1665 m
        assert 1500 < d < 1800


# ---------------------------------------------------------------------------
# match_to_facility
# ---------------------------------------------------------------------------


class TestMatchToFacility:
    def test_exact_match_at_facility_point(self) -> None:
        store = InMemoryProvenanceStore()
        f = make_facility(lat=30.36, lon=48.3)
        c = make_candidate(lat=30.36, lon=48.3)
        matched, dist = match_to_facility(c, [f], store=store)
        assert matched is f
        assert dist == pytest.approx(0.0, abs=1.0)

    def test_match_within_threshold(self) -> None:
        store = InMemoryProvenanceStore()
        f = make_facility(lat=30.36, lon=48.3)
        # ~44 m north — within 500 m default threshold
        c = make_candidate(lat=30.3604, lon=48.3)
        matched, dist = match_to_facility(c, [f], store=store)
        assert matched is f
        assert dist < DEFAULT_THRESHOLD_M

    def test_no_match_beyond_threshold(self) -> None:
        store = InMemoryProvenanceStore()
        f = make_facility(lat=30.36, lon=48.3)
        # ~1.1 km north — beyond 500 m
        c = make_candidate(lat=30.37, lon=48.3)
        matched, dist = match_to_facility(c, [f], store=store)
        assert matched is None
        assert dist > DEFAULT_THRESHOLD_M

    def test_empty_facility_list_returns_none_inf(self) -> None:
        store = InMemoryProvenanceStore()
        c = make_candidate()
        matched, dist = match_to_facility(c, [], store=store)
        assert matched is None
        assert dist == float("inf")

    def test_picks_nearest_when_multiple_facilities(self) -> None:
        store = InMemoryProvenanceStore()
        f_near = make_facility(lat=30.36, lon=48.3)  # on top of candidate
        f_far = make_facility(lat=32.0, lon=48.3)    # ~180 km north
        c = make_candidate(lat=30.36, lon=48.3)
        matched, dist = match_to_facility(c, [f_near, f_far], store=store)
        assert matched is f_near

    def test_custom_threshold(self) -> None:
        store = InMemoryProvenanceStore()
        f = make_facility(lat=30.36, lon=48.3)
        # ~220 m north: within 500 m but beyond 100 m threshold
        c = make_candidate(lat=30.362, lon=48.3)
        matched, _ = match_to_facility(c, [f], threshold_m=100.0, store=store)
        assert matched is None

    def test_polygon_facility_match_inside(self) -> None:
        store = InMemoryProvenanceStore()
        f = make_facility(lat=30.36, lon=48.3, as_polygon=True)
        c = make_candidate(lat=30.36, lon=48.3)
        matched, dist = match_to_facility(c, [f], store=store)
        assert matched is f
        assert dist == pytest.approx(0.0, abs=1.0)

    def test_provenance_emitted_on_match(self) -> None:
        store = InMemoryProvenanceStore()
        f = make_facility()
        c = make_candidate()
        match_to_facility(c, [f], store=store)
        assert len(store) == 1

    def test_provenance_emitted_on_no_match(self) -> None:
        store = InMemoryProvenanceStore()
        c = make_candidate()
        match_to_facility(c, [], store=store)
        assert len(store) == 1


# ---------------------------------------------------------------------------
# build_facility_tree / match_to_facility_with_tree
# ---------------------------------------------------------------------------


class TestBuildFacilityTree:
    def test_tree_match_equals_list_match(self) -> None:
        facilities = [
            make_facility(lat=30.36, lon=48.3),
            make_facility(lat=32.0, lon=48.3),
        ]
        candidate = make_candidate(lat=30.36, lon=48.3)

        store_a = InMemoryProvenanceStore()
        store_b = InMemoryProvenanceStore()

        matched_a, dist_a = match_to_facility(candidate, facilities, store=store_a)
        tree, facs = build_facility_tree(facilities)
        matched_b, dist_b = match_to_facility_with_tree(
            candidate, tree, facs, store=store_b
        )

        assert (matched_a is None) == (matched_b is None)
        assert dist_a == pytest.approx(dist_b, rel=1e-6)
