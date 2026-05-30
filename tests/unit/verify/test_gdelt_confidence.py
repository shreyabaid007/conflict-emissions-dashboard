"""Tests for GDELT corroboration in confidence assignment.

Verifies the confidence ceiling: GDELT match alone never produces CONFIRMED.
ACLED match overrides GDELT for same event. Both sources combined → CONFIRMED.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from wced.ai.classify import FireClassification, FireLabel
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.ingest.acled import ACLEDEvent
from wced.ingest.gdelt import GDELTEvent
from wced.models.event import DetectionSource
from wced.models.provenance import ConfidenceLabel
from wced.provenance.store import InMemoryProvenanceStore
from wced.verify.confidence import assign_confidence
from wced.verify.corroboration import CorroborationMatch
from wced.verify.sentinel2_check import VerificationStatus, VerifiedCandidate


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _candidate(n_overpasses: int = 2) -> CandidateFireEvent:
    t = datetime(2026, 3, 7, 9, 0, tzinfo=UTC)
    h = FIRMSDetection(
        latitude=32.66,
        longitude=51.68,
        frp_mw=80.0,
        detected_at=t,
        detection_source=DetectionSource.FIRMS_VIIRS,
        brightness_k=380.0,
        confidence="h",
        source_id=uuid4(),
    )
    return CandidateFireEvent(
        hotspots=(h,),
        centroid_lat=32.66,
        centroid_lon=51.68,
        first_detected_at=t,
        last_detected_at=t,
        peak_frp_mw=80.0,
        mean_frp_mw=80.0,
        n_overpasses=n_overpasses,
        provenance_id=uuid4(),
    )


def _s2_confirmed() -> VerifiedCandidate:
    clf = FireClassification(
        label=FireLabel.CONFIRMED_FIRE,
        confidence=0.92,
        rationale="Clear smoke plume and SWIR saturation.",
        provenance_id=uuid4(),
    )
    return VerifiedCandidate(
        candidate=_candidate(),
        status=VerificationStatus.VERIFIED,
        classification=clf,
    )


def _gdelt_match() -> CorroborationMatch:
    event = GDELTEvent(
        event_id="987654321",
        event_date=date(2026, 3, 7),
        event_type="190",
        event_root_code="19",
        actor1="ISRAEL",
        actor2="IRAN",
        latitude=32.661,
        longitude=51.680,
        source_url="https://reuters.com/article/123",
        num_articles=7,
        avg_tone=-5.2,
        goldstein_scale=-7.0,
        detected_at=datetime(2026, 3, 7, tzinfo=UTC),
    )
    return CorroborationMatch(event=event, source_type="gdelt", distance_m=150.0)


def _acled_match() -> CorroborationMatch:
    d = date(2026, 3, 7)
    event = ACLEDEvent(
        event_id_cnty="IRN5023",
        event_date=d,
        event_type="Explosions/Remote violence",
        sub_event_type="Air/drone strike",
        actor1="Military Forces of Israel",
        actor2="Government of Iran",
        country="Iran",
        location="Isfahan",
        latitude=32.661,
        longitude=51.680,
        source="Tehran Times",
        notes="Strike near oil refinery.",
        fatalities=0,
        timestamp=1741737600,
        iso=364,
        detected_at=datetime(d.year, d.month, d.day, tzinfo=UTC),
    )
    return CorroborationMatch(event=event, source_type="acled", distance_m=100.0)


# ---------------------------------------------------------------------------
# GDELT confidence ceiling tests
# ---------------------------------------------------------------------------


class TestGDELTConfidenceCeiling:
    """GDELT match alone never produces CONFIRMED."""

    def test_persistent_s2_gdelt_yields_verified_not_confirmed(self) -> None:
        """FIRMS persistent + S2 fire + GDELT match → VERIFIED (not CONFIRMED)."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_persistent_s2_acled_yields_confirmed(self) -> None:
        """FIRMS persistent + S2 fire + ACLED match → CONFIRMED."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_acled_match()],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED


class TestBothSourcesOverride:
    """ACLED match overrides GDELT for confidence when both present."""

    def test_both_acled_and_gdelt_yields_confirmed(self) -> None:
        """Both ACLED + GDELT match → CONFIRMED (ACLED dominates)."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_acled_match(), _gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED


class TestGDELTWithoutS2:
    """GDELT alone without S2 fire confirmation."""

    def test_persistent_no_s2_gdelt_yields_reported(self) -> None:
        """FIRMS persistent + GDELT + no S2 → REPORTED (not higher)."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            None,
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED

    def test_single_overpass_gdelt_yields_suspected(self) -> None:
        """Single FIRMS overpass + GDELT → SUSPECTED."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=1),
            None,
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.SUSPECTED


class TestProvenanceRecordsGDELT:
    """Provenance records include GDELT source type info."""

    def test_corroboration_source_types_recorded(self) -> None:
        store = InMemoryProvenanceStore()
        assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        rec = next(iter(store._store.values()))
        assert "gdelt" in rec.parameters["corroboration_source_types"]
        assert rec.parameters["has_gdelt"] is True
        assert rec.parameters["has_acled"] is False

    def test_both_source_types_recorded(self) -> None:
        store = InMemoryProvenanceStore()
        assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_acled_match(), _gdelt_match()],
            store=store,
        )
        rec = next(iter(store._store.values()))
        assert rec.parameters["has_acled"] is True
        assert rec.parameters["has_gdelt"] is True


class TestBackwardCompatibility:
    """Old-style acled_matches parameter still works."""

    def test_acled_matches_param_still_produces_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        d = date(2026, 3, 7)
        acled_ev = ACLEDEvent(
            event_id_cnty="IRN5023",
            event_date=d,
            event_type="Explosions/Remote violence",
            sub_event_type="Air/drone strike",
            actor1="A",
            actor2="B",
            country="Iran",
            location="Isfahan",
            latitude=32.661,
            longitude=51.680,
            source="AP",
            notes="Strike.",
            fatalities=0,
            timestamp=1741737600,
            iso=364,
            detected_at=datetime(d.year, d.month, d.day, tzinfo=UTC),
        )
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [acled_ev],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED
