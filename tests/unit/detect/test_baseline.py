"""Tests for wced.detect.baseline.

Covers:
- compute_baseline: normal window, empty window (fallback), active-event
  exclusion, single observation, window boundary conditions
- subtract_baseline: excess floored at zero, uncertainty propagation
- Property test (Hypothesis): fallback is returned whenever the qualifying
  observation list would be empty after filtering.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wced.detect.baseline import (
    FALLBACK_BASELINE_FRP_MW,
    FALLBACK_BASELINE_STD_MW,
    FacilityBaseline,
    compute_baseline,
    subtract_baseline,
)
from wced.provenance.store import InMemoryProvenanceStore

_T0 = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_obs(
    days_before: float,
    frp_mw: float,
    reference: datetime = _T0,
) -> tuple[datetime, float]:
    return reference - timedelta(days=days_before), frp_mw


# ---------------------------------------------------------------------------
# compute_baseline — normal cases
# ---------------------------------------------------------------------------


class TestComputeBaselineNormal:
    def test_p75_of_uniform_observations(self) -> None:
        store = InMemoryProvenanceStore()
        frps = [10.0, 20.0, 30.0, 40.0, 50.0]
        obs = [make_obs(i + 1, frp) for i, frp in enumerate(frps)]
        b = compute_baseline(uuid4(), obs, store=store, reference_time=_T0)
        assert b.baseline_frp_mw == pytest.approx(45.0)  # p75 of [10,20,30,40,50]
        assert b.n_observations == 5
        assert b.is_fallback is False

    def test_single_observation(self) -> None:
        store = InMemoryProvenanceStore()
        b = compute_baseline(
            uuid4(),
            [make_obs(5, 25.0)],
            store=store,
            reference_time=_T0,
        )
        assert b.baseline_frp_mw == pytest.approx(25.0)
        assert b.baseline_std_mw == pytest.approx(0.0)
        assert b.n_observations == 1
        assert b.is_fallback is False

    def test_robust_std_uses_iqr(self) -> None:
        store = InMemoryProvenanceStore()
        frps = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        obs = [make_obs(i + 1, frp) for i, frp in enumerate(frps)]
        b = compute_baseline(uuid4(), obs, store=store, reference_time=_T0)
        import statistics
        sorted_vals = sorted(frps)
        q1 = float(statistics.quantiles(sorted_vals, n=4)[0])
        q3 = float(statistics.quantiles(sorted_vals, n=4)[2])
        expected_std = (q3 - q1) / 1.349
        assert b.baseline_std_mw == pytest.approx(expected_std, rel=0.01)

    def test_observations_outside_window_excluded(self) -> None:
        store = InMemoryProvenanceStore()
        # 35 days ago — outside the 30-day window
        obs_old = [make_obs(35, 100.0)]
        obs_recent = [make_obs(5, 20.0)]
        b = compute_baseline(
            uuid4(), obs_old + obs_recent, store=store, reference_time=_T0
        )
        assert b.n_observations == 1
        assert b.baseline_frp_mw == pytest.approx(20.0)

    def test_active_event_window_excluded(self) -> None:
        store = InMemoryProvenanceStore()
        # Observations at days 1, 5, 10 before T0; active event covers day 1-3
        obs = [make_obs(1, 200.0), make_obs(5, 10.0), make_obs(10, 12.0)]
        event_start = _T0 - timedelta(days=3)
        event_end = _T0 - timedelta(hours=1)
        b = compute_baseline(
            uuid4(),
            obs,
            active_event_windows=[(event_start, event_end)],
            store=store,
            reference_time=_T0,
        )
        # The 200 MW observation at day 1 falls within the active event window
        # and should be excluded, leaving only [10.0, 12.0].
        assert b.n_observations == 2
        assert b.baseline_frp_mw == pytest.approx(12.5)  # p75 of [10, 12]

    def test_provenance_emitted(self) -> None:
        store = InMemoryProvenanceStore()
        b = compute_baseline(uuid4(), [make_obs(5, 20.0)], store=store, reference_time=_T0)
        assert len(store) == 1
        node = store.get(b.provenance_id)
        assert node.produced_by == "wced.detect.baseline"

    def test_facility_id_preserved(self) -> None:
        store = InMemoryProvenanceStore()
        fid = uuid4()
        b = compute_baseline(fid, [make_obs(5, 15.0)], store=store, reference_time=_T0)
        assert b.facility_id == fid

    def test_window_bounds_are_set(self) -> None:
        store = InMemoryProvenanceStore()
        b = compute_baseline(uuid4(), [make_obs(5, 10.0)], store=store, reference_time=_T0)
        expected_start = _T0 - timedelta(days=30)
        assert b.window_start == expected_start
        assert b.window_end == _T0


# ---------------------------------------------------------------------------
# compute_baseline — fallback (no historical data)
# ---------------------------------------------------------------------------


class TestComputeBaselineFallback:
    def test_empty_observations_returns_fallback(self) -> None:
        store = InMemoryProvenanceStore()
        b = compute_baseline(uuid4(), [], store=store, reference_time=_T0)
        assert b.is_fallback is True
        assert b.n_observations == 0
        assert b.baseline_frp_mw == pytest.approx(FALLBACK_BASELINE_FRP_MW)
        assert b.baseline_std_mw == pytest.approx(FALLBACK_BASELINE_STD_MW)

    def test_fallback_std_exceeds_mean(self) -> None:
        store = InMemoryProvenanceStore()
        b = compute_baseline(uuid4(), [], store=store, reference_time=_T0)
        # High-uncertainty estimate: std should be well above the mean.
        assert b.baseline_std_mw > b.baseline_frp_mw

    def test_fallback_provenance_is_suspected(self) -> None:
        store = InMemoryProvenanceStore()
        b = compute_baseline(uuid4(), [], store=store, reference_time=_T0)
        from wced.models.provenance import ConfidenceLabel
        node = store.get(b.provenance_id)
        assert node.confidence_label == ConfidenceLabel.SUSPECTED

    def test_all_observations_outside_window_gives_fallback(self) -> None:
        store = InMemoryProvenanceStore()
        # Everything 35+ days ago
        obs = [make_obs(35, 15.0), make_obs(40, 20.0)]
        b = compute_baseline(uuid4(), obs, store=store, reference_time=_T0)
        assert b.is_fallback is True

    def test_all_observations_in_active_event_window(self) -> None:
        store = InMemoryProvenanceStore()
        obs = [make_obs(1, 80.0), make_obs(2, 90.0)]
        # Active window covers the entire last 30 days
        active = [(_T0 - timedelta(days=30), _T0)]
        b = compute_baseline(uuid4(), obs, active_event_windows=active, store=store, reference_time=_T0)
        assert b.is_fallback is True


# ---------------------------------------------------------------------------
# subtract_baseline
# ---------------------------------------------------------------------------


class TestSubtractBaseline:
    def _make_baseline(self, frp: float, std: float, is_fallback: bool = False) -> FacilityBaseline:
        store = InMemoryProvenanceStore()
        if is_fallback:
            return compute_baseline(uuid4(), [], store=store, reference_time=_T0)
        obs = [(datetime(2026, 3, 10, tzinfo=UTC), frp)]
        return compute_baseline(uuid4(), obs, store=store, reference_time=_T0)

    def test_excess_above_baseline(self) -> None:
        b = self._make_baseline(10.0, 2.0)
        excess, uncertainty = subtract_baseline(25.0, b)
        assert excess == pytest.approx(25.0 - b.baseline_frp_mw)
        assert excess > 0

    def test_excess_floored_at_zero(self) -> None:
        # Candidate FRP below baseline → excess is 0, not negative
        b = self._make_baseline(30.0, 2.0)
        excess, uncertainty = subtract_baseline(5.0, b)
        assert excess == pytest.approx(0.0)

    def test_uncertainty_equals_baseline_std(self) -> None:
        b = self._make_baseline(10.0, 0.0)  # single obs → std = 0
        _, uncertainty = subtract_baseline(20.0, b)
        assert uncertainty == pytest.approx(b.baseline_std_mw)

    def test_fallback_baseline_yields_high_uncertainty(self) -> None:
        b = self._make_baseline(0.0, 0.0, is_fallback=True)
        _, uncertainty = subtract_baseline(100.0, b)
        assert uncertainty == pytest.approx(FALLBACK_BASELINE_STD_MW)

    def test_exact_baseline_frp_gives_zero_excess(self) -> None:
        store = InMemoryProvenanceStore()
        obs = [(datetime(2026, 3, 10, tzinfo=UTC), 20.0)]
        b = compute_baseline(uuid4(), obs, store=store, reference_time=_T0)
        excess, _ = subtract_baseline(b.baseline_frp_mw, b)
        assert excess == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Property test: fallback whenever qualifying observations are empty
# ---------------------------------------------------------------------------


@given(
    n_obs=st.integers(min_value=0, max_value=10),
    days_old=st.floats(min_value=31.0, max_value=100.0),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=80)
def test_fallback_iff_no_qualifying_observations(n_obs: int, days_old: float) -> None:
    """Baseline is always a fallback when all observations are outside the window."""
    store = InMemoryProvenanceStore()
    obs = [(_T0 - timedelta(days=days_old), float(i * 10 + 5)) for i in range(n_obs)]
    b = compute_baseline(uuid4(), obs, store=store, reference_time=_T0)
    # All observations are beyond 30 days → should be fallback regardless of n_obs.
    assert b.is_fallback is True
    assert b.n_observations == 0
    assert b.baseline_std_mw == pytest.approx(FALLBACK_BASELINE_STD_MW)
