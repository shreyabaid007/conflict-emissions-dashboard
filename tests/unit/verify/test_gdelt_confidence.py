"""Tests for the revised confidence decision table (v1.1 methodology).

GDELT is now the primary conflict-event source (ACLED dropped as a free
source). The confidence table no longer distinguishes ACLED from GDELT —
any corroboration source that passes spatial/temporal matching is treated
equally.

Decision table encoded here:

  | Persistent | S2 fire | Corroboration | → Label     |
  |------------|---------|---------------|-------------|
  | yes        | yes     | ≥1            | CONFIRMED   |
  | yes        | yes     | none          | VERIFIED    |
  | yes        | no      | ≥1            | VERIFIED    |
  | yes        | no      | none          | REPORTED    |
  | no         | *       | *             | SUSPECTED   |

CLAIMED is assigned externally (official statements only, never by this
function).

Each row in the table gets at least one test. Edge cases (S2 rejected,
S2 awaiting, backward-compat ``acled_matches`` param) are covered too.
"""
from __future__ import annotations

import logging
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


def _s2_rejected() -> VerifiedCandidate:
    clf = FireClassification(
        label=FireLabel.GAS_FLARING,
        confidence=0.80,
        rationale="Single-pixel flare stack signal.",
        provenance_id=uuid4(),
    )
    return VerifiedCandidate(
        candidate=_candidate(),
        status=VerificationStatus.REJECTED,
        classification=clf,
    )


def _s2_awaiting() -> VerifiedCandidate:
    return VerifiedCandidate(
        candidate=_candidate(),
        status=VerificationStatus.AWAITING_OPTICAL_CHECK,
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
# Row 1: persistent + S2 fire + corroboration → CONFIRMED
# ---------------------------------------------------------------------------


class TestConfirmedRow:
    """Persistent FIRMS + Sentinel-2 fire + ≥1 corroboration → CONFIRMED."""

    def test_gdelt_corroboration_reaches_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED

    def test_acled_corroboration_reaches_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_acled_match()],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED

    def test_both_sources_reaches_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_acled_match(), _gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED

    def test_multiple_gdelt_matches_reaches_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=3),
            _s2_confirmed(),
            [],
            corroboration_matches=[_gdelt_match(), _gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED


# ---------------------------------------------------------------------------
# Row 2: persistent + S2 fire + no corroboration → VERIFIED
# ---------------------------------------------------------------------------


class TestVerifiedS2Only:
    """Persistent FIRMS + S2 fire + no corroboration → VERIFIED."""

    def test_s2_fire_no_corroboration_yields_verified(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_s2_fire_no_corroboration_matches_none_yields_verified(self) -> None:
        """corroboration_matches=None + empty acled_matches → VERIFIED."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED


# ---------------------------------------------------------------------------
# Row 3: persistent + no S2 fire + corroboration → VERIFIED
# ---------------------------------------------------------------------------


class TestVerifiedCorroborationNoS2:
    """Persistent FIRMS + corroboration + no S2 fire → VERIFIED."""

    def test_gdelt_no_s2_yields_verified(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            None,
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_gdelt_s2_rejected_yields_verified(self) -> None:
        """S2 ran but returned flaring (not fire) — corroboration still lifts."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_rejected(),
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_gdelt_s2_awaiting_yields_verified(self) -> None:
        """S2 not yet checked (clouds) — corroboration still lifts."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_awaiting(),
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_acled_no_s2_yields_verified(self) -> None:
        """Backward compat: ACLED corroboration without S2 also → VERIFIED."""
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            None,
            [],
            corroboration_matches=[_acled_match()],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED


# ---------------------------------------------------------------------------
# Row 4: persistent + no S2 fire + no corroboration → REPORTED
# ---------------------------------------------------------------------------


class TestReportedRow:
    """Persistent FIRMS only (no S2 fire, no corroboration) → REPORTED."""

    def test_persistent_no_s2_no_corroboration_yields_reported(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            None,
            [],
            corroboration_matches=[],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED

    def test_s2_rejected_no_corroboration_yields_reported(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_rejected(),
            [],
            corroboration_matches=[],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED

    def test_s2_awaiting_no_corroboration_yields_reported(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_awaiting(),
            [],
            corroboration_matches=[],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED


# ---------------------------------------------------------------------------
# Row 5: not persistent → SUSPECTED (regardless of other signals)
# ---------------------------------------------------------------------------


class TestSuspectedRow:
    """Single FIRMS overpass → SUSPECTED regardless of S2 or corroboration."""

    def test_single_overpass_bare_yields_suspected(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=1),
            None,
            [],
            corroboration_matches=[],
            store=store,
        )
        assert result is ConfidenceLabel.SUSPECTED

    def test_single_overpass_with_s2_fire_yields_suspected(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=1),
            _s2_confirmed(),
            [],
            corroboration_matches=[],
            store=store,
        )
        assert result is ConfidenceLabel.SUSPECTED

    def test_single_overpass_with_gdelt_yields_suspected(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=1),
            None,
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.SUSPECTED

    def test_single_overpass_with_s2_and_gdelt_yields_suspected(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=1),
            _s2_confirmed(),
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        assert result is ConfidenceLabel.SUSPECTED

    def test_single_overpass_corroboration_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryProvenanceStore()
        cand = _candidate(n_overpasses=1)
        with caplog.at_level(logging.WARNING, logger="wced.verify.confidence"):
            assign_confidence(
                cand, None, [],
                corroboration_matches=[_gdelt_match()],
                store=store,
            )
        assert any("near-miss" in r.message for r in caplog.records)
        assert any(str(cand.id) in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Provenance recording
# ---------------------------------------------------------------------------


class TestProvenanceRecording:
    """Provenance records capture source type information correctly."""

    def test_gdelt_source_type_recorded(self) -> None:
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

    def test_gdelt_event_id_recorded(self) -> None:
        store = InMemoryProvenanceStore()
        gm = _gdelt_match()
        assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[gm],
            store=store,
        )
        rec = next(iter(store._store.values()))
        assert gm.event.event_id in rec.parameters["corroboration_event_ids"]

    def test_confirmed_label_stored_in_provenance(self) -> None:
        store = InMemoryProvenanceStore()
        assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            corroboration_matches=[_gdelt_match()],
            store=store,
        )
        rec = next(iter(store._store.values()))
        assert rec.confidence_label is ConfidenceLabel.CONFIRMED


# ---------------------------------------------------------------------------
# Backward compatibility — old acled_matches parameter
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Old-style acled_matches parameter (no corroboration_matches) still works."""

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

    def test_acled_matches_no_s2_yields_verified(self) -> None:
        """Old path: ACLED match without S2 now → VERIFIED (not REPORTED)."""
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
            _s2_awaiting(),
            [acled_ev],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED
