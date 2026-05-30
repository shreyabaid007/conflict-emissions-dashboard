"""Tests for the OilFuelFireCategory implementation.

Verifies that the category protocol wrapper produces identical outputs to
the underlying detect/verify/quantify modules.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import numpy as np
import pytest

from wced.categories.base import (
    CategoryRegistry,
    DetectionEvent,
    EmissionCategory,
    VerificationResult,
    get_registry,
    reset_registry,
)
from wced.categories.oil_fuel_fire.category import OilFuelFireCategory, _estimate_frp_integral
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.models.event import DetectionSource
from wced.models.facility import Facility, FacilityType
from wced.models.provenance import ConfidenceLabel
from wced.provenance.store import InMemoryProvenanceStore
from wced.verify.confidence import assign_confidence
from wced.verify.corroboration import CorroborationMatch
from wced.verify.sentinel2_check import VerificationStatus, VerifiedCandidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_detection(
    lat: float = 32.65,
    lon: float = 51.68,
    frp: float = 50.0,
    dt: datetime | None = None,
) -> FIRMSDetection:
    return FIRMSDetection(
        id=uuid4(),
        latitude=lat,
        longitude=lon,
        frp_mw=frp,
        detected_at=dt or datetime(2026, 3, 15, 12, 0, tzinfo=UTC),
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=400.0,
        confidence="h",
        source_id=uuid4(),
    )


def _make_candidate(n_hotspots: int = 3) -> CandidateFireEvent:
    base_t = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    hotspots = tuple(
        _make_detection(
            frp=50.0 + i * 10,
            dt=base_t + timedelta(hours=6 * i),
        )
        for i in range(n_hotspots)
    )
    return CandidateFireEvent(
        hotspots=hotspots,
        centroid_lat=32.65,
        centroid_lon=51.68,
        first_detected_at=hotspots[0].detected_at,
        last_detected_at=hotspots[-1].detected_at,
        peak_frp_mw=max(h.frp_mw for h in hotspots),
        mean_frp_mw=float(np.mean([h.frp_mw for h in hotspots])),
        n_overpasses=n_hotspots,
        provenance_id=uuid4(),
    )


def _make_facility() -> Facility:
    return Facility(
        id=uuid4(),
        name="Isfahan Refinery",
        facility_type=FacilityType.REFINERY,
        geometry_wkt="POINT(51.68 32.65)",
        country="IRN",
        capacity_barrels=350000,
        source_url="https://example.com/isfahan",
        added_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_implements_emission_category(self):
        cat = OilFuelFireCategory()
        assert isinstance(cat, EmissionCategory)

    def test_id(self):
        assert OilFuelFireCategory().id == "oil_fuel_fire"

    def test_methodology_version(self):
        assert OilFuelFireCategory().methodology_version == "1.1.0"

    def test_required_sources(self):
        sources = OilFuelFireCategory().required_sources()
        names = {s.name for s in sources}
        assert "firms_viirs" in names

    def test_discoverable_in_registry(self):
        reset_registry()
        registry = get_registry()
        assert "oil_fuel_fire" in registry
        reset_registry()


# ---------------------------------------------------------------------------
# Detect
# ---------------------------------------------------------------------------


class TestDetect:
    def test_detect_empty_returns_empty(self):
        cat = OilFuelFireCategory()
        events = cat.detect({"firms_detections": [], "facilities": []})
        assert events == []

    def test_detect_produces_detection_events(self):
        cat = OilFuelFireCategory()
        detections = [
            _make_detection(frp=50, dt=datetime(2026, 3, 15, 12, 0, tzinfo=UTC)),
            _make_detection(frp=60, dt=datetime(2026, 3, 15, 18, 0, tzinfo=UTC)),
        ]
        facility = _make_facility()
        store = InMemoryProvenanceStore()

        events = cat.detect({
            "firms_detections": detections,
            "facilities": [facility],
            "provenance_store": store,
        })

        assert len(events) >= 1
        assert events[0].category_id == "oil_fuel_fire"
        assert "candidate" in events[0].data
        assert "facility" in events[0].data


# ---------------------------------------------------------------------------
# Verify — numeric equivalence with direct assign_confidence
# ---------------------------------------------------------------------------


class TestVerify:
    def test_verify_matches_direct_confidence_assignment(self):
        """Category.verify() must produce the same label as assign_confidence()."""
        cat = OilFuelFireCategory()
        candidate = _make_candidate(n_hotspots=3)
        facility = _make_facility()

        det_event = DetectionEvent(
            event_id=str(candidate.id),
            category_id="oil_fuel_fire",
            data={
                "candidate": candidate,
                "facility": facility,
                "match_distance_m": 100.0,
                "detection_hash": "abc123",
            },
        )

        # No S2, no corroboration → should be REPORTED (persistent but no confirmation)
        store_direct = InMemoryProvenanceStore()
        direct_label = assign_confidence(
            candidate,
            None,
            [],
            corroboration_matches=[],
            store=store_direct,
        )

        store_cat = InMemoryProvenanceStore()
        result = cat.verify(det_event, {
            "verified_candidates": {},
            "corroboration_matches": {},
            "provenance_store": store_cat,
        })

        assert result.confidence_label == direct_label.value

    def test_verify_single_overpass_suspected(self):
        cat = OilFuelFireCategory()
        candidate = _make_candidate(n_hotspots=1)

        det_event = DetectionEvent(
            event_id=str(candidate.id),
            category_id="oil_fuel_fire",
            data={"candidate": candidate, "facility": None},
        )

        result = cat.verify(det_event, {})
        assert result.confidence_label == ConfidenceLabel.SUSPECTED.value
        assert result.verified is False


# ---------------------------------------------------------------------------
# FRP integral helper
# ---------------------------------------------------------------------------


class TestFrpIntegral:
    def test_single_overpass_returns_none(self):
        candidate = _make_candidate(n_hotspots=1)
        assert _estimate_frp_integral(candidate) is None

    def test_two_overpasses_trapezoidal(self):
        base_t = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
        h1 = _make_detection(frp=40.0, dt=base_t)
        h2 = _make_detection(frp=60.0, dt=base_t + timedelta(hours=6))
        candidate = CandidateFireEvent(
            hotspots=(h1, h2),
            centroid_lat=32.65,
            centroid_lon=51.68,
            first_detected_at=h1.detected_at,
            last_detected_at=h2.detected_at,
            peak_frp_mw=60.0,
            mean_frp_mw=50.0,
            n_overpasses=2,
            provenance_id=uuid4(),
        )
        integral = _estimate_frp_integral(candidate)
        assert integral is not None
        # avg FRP = 50 MW, dt = 6h = 21600s, integral = 50 * 21600 = 1_080_000 MJ
        assert abs(integral - 1_080_000.0) < 1e-6
