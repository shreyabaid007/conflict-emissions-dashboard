"""Tests for wced.quantify.aggregate.

Property tests and worked examples for all five aggregation functions.
Hand-computed invariants follow methodology v1.0 §3.5 aggregation rules.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import numpy as np
import pytest

from wced.models.event import DetectionSource, FireEvent
from wced.models.facility import Facility, FacilityType
from wced.models.provenance import ConfidenceLabel
from wced.quantify.aggregate import (
    aggregate_by_country,
    aggregate_by_facility,
    aggregate_by_facility_type,
    aggregate_cumulative,
    aggregate_daily,
)
from wced.quantify.distribution import Distribution
from wced.quantify.reconcile import ReconciliationResult


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

_DAY_A = date(2026, 3, 1)
_DAY_B = date(2026, 3, 2)


def _ts(d: date, hour: int = 12) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=UTC)


def _event(
    *,
    facility_id: UUID | None = None,
    detected: date = _DAY_A,
) -> FireEvent:
    t0 = _ts(detected)
    return FireEvent(
        facility_id=facility_id or uuid4(),
        detected_at=t0,
        last_seen_at=t0 + timedelta(hours=6),
        peak_frp_mw=100.0,
        total_frp_integral_mj=1000.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.REPORTED,
        provenance_id=uuid4(),
        created_at=t0,
        updated_at=t0,
    )


def _dist(p50: float, n: int = 10_000) -> Distribution:
    """Normal distribution tightly centred on *p50* (CV=10%)."""
    rng = np.random.default_rng(seed=int(p50) % (2**32))
    samples = rng.normal(p50, p50 * 0.10, n)
    return Distribution.from_samples(
        samples, units="tCO2e", methodology_version="1.0", provenance_id=uuid4()
    )


def _ok_result(dist: Distribution) -> ReconciliationResult:
    """ReconciliationResult that passes the eligibility gate."""
    return ReconciliationResult(
        final_distribution=dist,
        frp_estimate=dist,
        inventory_estimate=None,
        reported_estimate=None,
        agreement_ratio=None,
        reconciled_ok=True,
        near_boundary=False,
        needs_review=False,
        review_reason=None,
    )


def _review_result(dist: Distribution) -> ReconciliationResult:
    """ReconciliationResult that FAILS the eligibility gate (needs_review)."""
    return ReconciliationResult(
        final_distribution=None,
        frp_estimate=dist,
        inventory_estimate=None,
        reported_estimate=None,
        agreement_ratio=3.5,
        reconciled_ok=False,
        near_boundary=False,
        needs_review=True,
        review_reason="ρ out of band",
    )


def _facility(
    *,
    facility_id: UUID | None = None,
    country: str = "IRN",
    ftype: FacilityType = FacilityType.REFINERY,
) -> Facility:
    return Facility(
        id=facility_id or uuid4(),
        name="Test Facility",
        facility_type=ftype,
        geometry_wkt="POINT (48.0 32.0)",
        country=country,
        source_url="https://example.com/registry",
        added_at=_ts(_DAY_A),
    )


# ---------------------------------------------------------------------------
# aggregate_daily
# ---------------------------------------------------------------------------


class TestAggregateDaily:
    def test_empty_input_returns_zero(self) -> None:
        result = aggregate_daily([], _DAY_A)
        assert result.p50 == 0.0
        assert result.units == "tCO2e"
        assert result.methodology_version == "1.0.5"

    def test_no_events_on_day_returns_zero(self) -> None:
        dist = _dist(1_000.0)
        pairs = [(_event(detected=_DAY_B), _ok_result(dist))]
        result = aggregate_daily(pairs, _DAY_A)
        assert result.p50 == 0.0

    def test_single_event_matches_its_p50(self) -> None:
        dist = _dist(5_000.0)
        pairs = [(_event(detected=_DAY_A), _ok_result(dist))]
        result = aggregate_daily(pairs, _DAY_A)
        # Resampled single distribution: p50 should be within 5% of original.
        assert abs(result.p50 - dist.p50) / dist.p50 < 0.05

    def test_filters_events_to_correct_day(self) -> None:
        dist_a = _dist(10_000.0)
        dist_b = _dist(10_000.0)
        pairs = [
            (_event(detected=_DAY_A), _ok_result(dist_a)),
            (_event(detected=_DAY_B), _ok_result(dist_b)),
        ]
        result = aggregate_daily(pairs, _DAY_A)
        # Only DAY_A event included — result p50 ≈ 10_000, not ≈ 20_000.
        assert result.p50 < 15_000

    def test_needs_review_excluded(self) -> None:
        dist = _dist(10_000.0)
        review_dist = _dist(50_000.0)
        pairs = [
            (_event(detected=_DAY_A), _ok_result(dist)),
            (_event(detected=_DAY_A), _review_result(review_dist)),
        ]
        result = aggregate_daily(pairs, _DAY_A)
        # Only the 10_000 event contributes; 50_000 is excluded.
        assert result.p50 < 20_000

    def test_sum_of_n_identical_distributions(self) -> None:
        """Property: N identical distributions → p50 ≈ N × single p50."""
        n = 5
        p50_each = 1_000.0
        dist = _dist(p50_each)
        pairs = [(_event(detected=_DAY_A), _ok_result(dist)) for _ in range(n)]
        result = aggregate_daily(pairs, _DAY_A)
        expected = n * p50_each
        assert abs(result.p50 - expected) / expected < 0.10, (
            f"Expected p50 ≈ {expected}, got {result.p50:.1f}"
        )

    def test_result_units_and_version(self) -> None:
        dist = _dist(1_000.0)
        result = aggregate_daily([(_event(detected=_DAY_A), _ok_result(dist))], _DAY_A)
        assert result.units == "tCO2e"
        assert result.methodology_version == "1.0"

    def test_result_has_samples(self) -> None:
        dist = _dist(1_000.0)
        result = aggregate_daily([(_event(detected=_DAY_A), _ok_result(dist))], _DAY_A)
        assert result.samples is not None
        assert len(result.samples) == 10_000

    def test_percentile_invariant(self) -> None:
        pairs = [(_event(detected=_DAY_A), _ok_result(_dist(float(i * 1000)))) for i in range(1, 4)]
        result = aggregate_daily(pairs, _DAY_A)
        assert result.p5 <= result.p50 <= result.p95

    def test_reproducible_provenance(self) -> None:
        """Same inputs produce the same provenance_id."""
        dist = _dist(1_000.0)
        ev = _event(detected=_DAY_A)
        pairs = [(ev, _ok_result(dist))]
        r1 = aggregate_daily(pairs, _DAY_A)
        r2 = aggregate_daily(pairs, _DAY_A)
        assert r1.provenance_id == r2.provenance_id


# ---------------------------------------------------------------------------
# aggregate_cumulative
# ---------------------------------------------------------------------------


class TestAggregateCumulative:
    def test_empty_input_returns_zero(self) -> None:
        result = aggregate_cumulative([], _DAY_B)
        assert result.p50 == 0.0

    def test_includes_events_on_until_date(self) -> None:
        dist = _dist(5_000.0)
        pairs = [(_event(detected=_DAY_B), _ok_result(dist))]
        result = aggregate_cumulative(pairs, until_date=_DAY_B)
        assert result.p50 > 0.0

    def test_excludes_events_after_until_date(self) -> None:
        dist = _dist(5_000.0)
        pairs = [(_event(detected=_DAY_B), _ok_result(dist))]
        result = aggregate_cumulative(pairs, until_date=_DAY_A)
        assert result.p50 == 0.0

    def test_multi_day_sum(self) -> None:
        """Events on both days both contribute to cumulative total."""
        dist = _dist(10_000.0)
        pairs = [
            (_event(detected=_DAY_A), _ok_result(dist)),
            (_event(detected=_DAY_B), _ok_result(dist)),
        ]
        result = aggregate_cumulative(pairs, until_date=_DAY_B)
        # Two events of ~10_000 each → cumulative p50 ≈ 20_000.
        assert result.p50 > 15_000

    def test_needs_review_excluded_from_cumulative(self) -> None:
        dist = _dist(10_000.0)
        big = _dist(100_000.0)
        pairs = [
            (_event(detected=_DAY_A), _ok_result(dist)),
            (_event(detected=_DAY_A), _review_result(big)),
        ]
        result = aggregate_cumulative(pairs, until_date=_DAY_B)
        assert result.p50 < 20_000

    def test_sum_of_n_identical_distributions(self) -> None:
        """Property: N identical distributions → p50 ≈ N × single p50."""
        n = 4
        p50_each = 2_500.0
        dist = _dist(p50_each)
        pairs = [(_event(detected=_DAY_A), _ok_result(dist)) for _ in range(n)]
        result = aggregate_cumulative(pairs, until_date=_DAY_B)
        expected = n * p50_each
        assert abs(result.p50 - expected) / expected < 0.10


# ---------------------------------------------------------------------------
# aggregate_by_facility
# ---------------------------------------------------------------------------


class TestAggregateByFacility:
    def test_empty_input_returns_empty_dict(self) -> None:
        assert aggregate_by_facility([]) == {}

    def test_single_event(self) -> None:
        fid = uuid4()
        dist = _dist(5_000.0)
        pairs = [(_event(facility_id=fid), _ok_result(dist))]
        result = aggregate_by_facility(pairs)
        assert set(result.keys()) == {fid}
        assert abs(result[fid].p50 - dist.p50) / dist.p50 < 0.05

    def test_groups_by_facility(self) -> None:
        fid1, fid2 = uuid4(), uuid4()
        dist = _dist(10_000.0)
        pairs = [
            (_event(facility_id=fid1), _ok_result(dist)),
            (_event(facility_id=fid2), _ok_result(dist)),
            (_event(facility_id=fid1), _ok_result(dist)),
        ]
        result = aggregate_by_facility(pairs)
        assert set(result.keys()) == {fid1, fid2}
        # fid1 has two events → p50 ≈ 20_000; fid2 has one → p50 ≈ 10_000.
        assert result[fid1].p50 > result[fid2].p50

    def test_needs_review_excluded(self) -> None:
        fid = uuid4()
        dist = _dist(10_000.0)
        big = _dist(100_000.0)
        pairs = [
            (_event(facility_id=fid), _ok_result(dist)),
            (_event(facility_id=fid), _review_result(big)),
        ]
        result = aggregate_by_facility(pairs)
        assert fid in result
        assert result[fid].p50 < 20_000

    def test_all_needs_review_returns_empty_dict(self) -> None:
        fid = uuid4()
        big = _dist(100_000.0)
        pairs = [(_event(facility_id=fid), _review_result(big))]
        result = aggregate_by_facility(pairs)
        assert result == {}

    def test_sum_property_per_facility(self) -> None:
        """Property: N identical events for a facility → p50 ≈ N × single."""
        n = 3
        fid = uuid4()
        p50_each = 3_000.0
        dist = _dist(p50_each)
        pairs = [(_event(facility_id=fid), _ok_result(dist)) for _ in range(n)]
        result = aggregate_by_facility(pairs)
        expected = n * p50_each
        assert abs(result[fid].p50 - expected) / expected < 0.10

    def test_result_units_and_version(self) -> None:
        fid = uuid4()
        dist = _dist(1_000.0)
        result = aggregate_by_facility([(_event(facility_id=fid), _ok_result(dist))])
        assert result[fid].units == "tCO2e"
        assert result[fid].methodology_version == "1.0"


# ---------------------------------------------------------------------------
# aggregate_by_country
# ---------------------------------------------------------------------------


class TestAggregateByCountry:
    def test_empty_input_returns_empty_dict(self) -> None:
        assert aggregate_by_country([], {}) == {}

    def test_groups_by_country(self) -> None:
        fid_irn, fid_isr = uuid4(), uuid4()
        f_irn = _facility(facility_id=fid_irn, country="IRN")
        f_isr = _facility(facility_id=fid_isr, country="ISR")
        fmap = {fid_irn: f_irn, fid_isr: f_isr}

        dist = _dist(10_000.0)
        pairs = [
            (_event(facility_id=fid_irn), _ok_result(dist)),
            (_event(facility_id=fid_isr), _ok_result(dist)),
        ]
        result = aggregate_by_country(pairs, fmap)
        assert set(result.keys()) == {"IRN", "ISR"}

    def test_missing_facility_excluded(self) -> None:
        fid = uuid4()
        dist = _dist(10_000.0)
        pairs = [(_event(facility_id=fid), _ok_result(dist))]
        # Empty facility_map: event has no country attribution.
        result = aggregate_by_country(pairs, {})
        assert result == {}

    def test_needs_review_excluded(self) -> None:
        fid = uuid4()
        fmap = {fid: _facility(facility_id=fid, country="IRN")}
        dist = _dist(10_000.0)
        big = _dist(100_000.0)
        pairs = [
            (_event(facility_id=fid), _ok_result(dist)),
            (_event(facility_id=fid), _review_result(big)),
        ]
        result = aggregate_by_country(pairs, fmap)
        assert result["IRN"].p50 < 20_000

    def test_sum_property(self) -> None:
        """N events in same country → p50 ≈ N × single p50."""
        n = 4
        fid = uuid4()
        fmap = {fid: _facility(facility_id=fid, country="IRN")}
        p50_each = 5_000.0
        dist = _dist(p50_each)
        pairs = [(_event(facility_id=fid), _ok_result(dist)) for _ in range(n)]
        result = aggregate_by_country(pairs, fmap)
        expected = n * p50_each
        assert abs(result["IRN"].p50 - expected) / expected < 0.10

    def test_result_units(self) -> None:
        fid = uuid4()
        fmap = {fid: _facility(facility_id=fid, country="IRN")}
        dist = _dist(1_000.0)
        result = aggregate_by_country([(_event(facility_id=fid), _ok_result(dist))], fmap)
        assert result["IRN"].units == "tCO2e"


# ---------------------------------------------------------------------------
# aggregate_by_facility_type
# ---------------------------------------------------------------------------


class TestAggregateByFacilityType:
    def test_empty_input_returns_empty_dict(self) -> None:
        assert aggregate_by_facility_type([], {}) == {}

    def test_groups_by_type(self) -> None:
        fid_ref, fid_dep = uuid4(), uuid4()
        fmap = {
            fid_ref: _facility(facility_id=fid_ref, ftype=FacilityType.REFINERY),
            fid_dep: _facility(facility_id=fid_dep, ftype=FacilityType.OIL_DEPOT),
        }
        dist = _dist(10_000.0)
        pairs = [
            (_event(facility_id=fid_ref), _ok_result(dist)),
            (_event(facility_id=fid_dep), _ok_result(dist)),
        ]
        result = aggregate_by_facility_type(pairs, fmap)
        assert set(result.keys()) == {FacilityType.REFINERY, FacilityType.OIL_DEPOT}

    def test_missing_facility_excluded(self) -> None:
        fid = uuid4()
        dist = _dist(10_000.0)
        pairs = [(_event(facility_id=fid), _ok_result(dist))]
        result = aggregate_by_facility_type(pairs, {})
        assert result == {}

    def test_needs_review_excluded(self) -> None:
        fid = uuid4()
        fmap = {fid: _facility(facility_id=fid, ftype=FacilityType.REFINERY)}
        dist = _dist(10_000.0)
        big = _dist(100_000.0)
        pairs = [
            (_event(facility_id=fid), _ok_result(dist)),
            (_event(facility_id=fid), _review_result(big)),
        ]
        result = aggregate_by_facility_type(pairs, fmap)
        assert result[FacilityType.REFINERY].p50 < 20_000

    def test_sum_property(self) -> None:
        """N events of same type → p50 ≈ N × single p50."""
        n = 6
        fid = uuid4()
        fmap = {fid: _facility(facility_id=fid, ftype=FacilityType.REFINERY)}
        p50_each = 2_000.0
        dist = _dist(p50_each)
        pairs = [(_event(facility_id=fid), _ok_result(dist)) for _ in range(n)]
        result = aggregate_by_facility_type(pairs, fmap)
        expected = n * p50_each
        assert abs(result[FacilityType.REFINERY].p50 - expected) / expected < 0.10

    def test_result_units_and_version(self) -> None:
        fid = uuid4()
        fmap = {fid: _facility(facility_id=fid, ftype=FacilityType.REFINERY)}
        dist = _dist(1_000.0)
        result = aggregate_by_facility_type(
            [(_event(facility_id=fid), _ok_result(dist))], fmap
        )
        assert result[FacilityType.REFINERY].units == "tCO2e"
        assert result[FacilityType.REFINERY].methodology_version == "1.0"


# ---------------------------------------------------------------------------
# Cross-function property: version mismatch raises
# ---------------------------------------------------------------------------


def test_mixed_methodology_versions_raise() -> None:
    """Aggregating Distributions from different methodology versions is an error."""
    rng = np.random.default_rng(0)
    dist_v1 = Distribution.from_samples(
        rng.normal(1000, 100, 100),
        units="tCO2e",
        methodology_version="1.0",
        provenance_id=uuid4(),
    )
    dist_v2 = Distribution.from_samples(
        rng.normal(1000, 100, 100),
        units="tCO2e",
        methodology_version="2.0",
        provenance_id=uuid4(),
    )
    pairs = [
        (_event(detected=_DAY_A), _ok_result(dist_v1)),
        (_event(detected=_DAY_A), _ok_result(dist_v2)),
    ]
    with pytest.raises(ValueError, match="methodology versions"):
        aggregate_daily(pairs, _DAY_A)
