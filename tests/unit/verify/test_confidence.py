"""Tests for wced.verify.confidence — one test per decision-table row.

Each test builds the minimal fixture needed to exercise exactly one branch of
the label assignment logic. All tests are offline (no network, no Anthropic).
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from wced.ai.classify import FireClassification, FireLabel
from wced.detect.hotspot import CandidateFireEvent, FIRMSDetection
from wced.ingest.acled import ACLEDEvent
from wced.models.event import DetectionSource
from wced.models.provenance import ConfidenceLabel
from wced.provenance.store import InMemoryProvenanceStore
from wced.verify.confidence import assign_confidence
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


def _acled_match() -> ACLEDEvent:
    d = date(2026, 3, 7)
    return ACLEDEvent(
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


# ---------------------------------------------------------------------------
# Decision table — one test per rule
# ---------------------------------------------------------------------------


class TestConfirmed:
    """FIRMS persistent + S2 fire confirmed + ACLED match → CONFIRMED."""

    def test_all_three_sources_yields_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [_acled_match()],
            store=store,
        )
        assert result is ConfidenceLabel.CONFIRMED

    def test_provenance_record_written_with_confirmed(self) -> None:
        store = InMemoryProvenanceStore()
        cand = _candidate(n_overpasses=2)
        assign_confidence(cand, _s2_confirmed(), [_acled_match()], store=store)
        assert len(store) == 1
        rec = next(iter(store._store.values()))
        assert rec.confidence_label is ConfidenceLabel.CONFIRMED
        assert rec.parameters["n_corroboration_matches"] == 1


class TestVerified:
    """FIRMS persistent + S2 fire confirmed, no ACLED → VERIFIED."""

    def test_no_acled_yields_verified(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_confirmed(),
            [],
            store=store,
        )
        assert result is ConfidenceLabel.VERIFIED

    def test_s2_result_parameters_recorded(self) -> None:
        store = InMemoryProvenanceStore()
        cand = _candidate(n_overpasses=2)
        assign_confidence(cand, _s2_confirmed(), [], store=store)
        rec = next(iter(store._store.values()))
        assert rec.parameters["s2_confirms_fire"] is True
        assert rec.parameters["persistent"] is True


class TestReported:
    """FIRMS persistent, no optical confirmation → REPORTED."""

    def test_cloudy_s2_yields_reported(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_awaiting(),
            [],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED

    def test_s2_rejected_yields_reported(self) -> None:
        # Optical ran but returned flaring — still no fire confirmation.
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_rejected(),
            [],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED

    def test_s2_none_persistent_yields_reported(self) -> None:
        # s2_result=None means the check hasn't been attempted yet.
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=3),
            None,
            [],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED

    def test_reported_with_acled_but_no_s2_fire(self) -> None:
        # ACLED alone can't confirm fire — stays REPORTED (not CONFIRMED).
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=2),
            _s2_awaiting(),
            [_acled_match()],
            store=store,
        )
        assert result is ConfidenceLabel.REPORTED


class TestSuspected:
    """Single-overpass candidates → SUSPECTED regardless of other inputs."""

    def test_single_overpass_no_corroboration_yields_suspected(self) -> None:
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=1),
            None,
            [],
            store=store,
        )
        assert result is ConfidenceLabel.SUSPECTED

    def test_single_overpass_with_s2_fire_still_suspected(self) -> None:
        # Even if S2 saw fire, one FIRMS pass does not satisfy the persistence
        # requirement — keep as SUSPECTED pending a second overpass.
        store = InMemoryProvenanceStore()
        result = assign_confidence(
            _candidate(n_overpasses=1),
            _s2_confirmed(),
            [],
            store=store,
        )
        assert result is ConfidenceLabel.SUSPECTED


class TestACLEDOnlyEdgeCase:
    """ACLED strike reported but no FIRMS hotspot — must NOT auto-promote."""

    def test_acled_without_firms_yields_suspected_not_confirmed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryProvenanceStore()
        with caplog.at_level(logging.WARNING, logger="wced.verify.confidence"):
            result = assign_confidence(
                _candidate(n_overpasses=1),   # single overpass = not persistent
                None,
                [_acled_match()],             # ACLED says strike happened
                store=store,
            )
        assert result is ConfidenceLabel.SUSPECTED
        assert any("near-miss" in r.message for r in caplog.records)

    def test_warning_message_names_candidate_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryProvenanceStore()
        cand = _candidate(n_overpasses=1)
        with caplog.at_level(logging.WARNING, logger="wced.verify.confidence"):
            assign_confidence(cand, None, [_acled_match()], store=store)
        assert any(str(cand.id) in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_exactly_one_record_written_per_call(self) -> None:
        store = InMemoryProvenanceStore()
        assign_confidence(_candidate(2), _s2_confirmed(), [_acled_match()], store=store)
        assign_confidence(_candidate(1), None, [], store=store)
        assert len(store) == 2

    def test_record_inputs_include_candidate_provenance_id(self) -> None:
        store = InMemoryProvenanceStore()
        cand = _candidate(n_overpasses=2)
        assign_confidence(cand, _s2_confirmed(), [], store=store)
        rec = next(iter(store._store.values()))
        assert cand.provenance_id in rec.inputs

    def test_method_is_versioned(self) -> None:
        store = InMemoryProvenanceStore()
        assign_confidence(_candidate(2), None, [], store=store)
        rec = next(iter(store._store.values()))
        assert rec.method == "confidence_assignment_v1.1"

    def test_acled_event_ids_recorded_in_parameters(self) -> None:
        store = InMemoryProvenanceStore()
        cand = _candidate(n_overpasses=2)
        ev = _acled_match()
        assign_confidence(cand, _s2_confirmed(), [ev], store=store)
        rec = next(iter(store._store.values()))
        assert ev.event_id_cnty in rec.parameters["corroboration_event_ids"]
