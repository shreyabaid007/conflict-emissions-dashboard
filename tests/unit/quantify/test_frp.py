"""Tests for wced.quantify.frp.compute_frp_emissions.

Hand-computed expected values are derived from the methodology v1.0 §3.3
equations applied at the mean of each input distribution.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import numpy as np
import pytest

from wced.detect.baseline import FacilityBaseline, compute_baseline
from wced.models.event import DetectionSource, FireEvent
from wced.models.provenance import ConfidenceLabel
from wced.provenance.store import InMemoryProvenanceStore
from wced.quantify.factors import load_factors, load_parameter_distributions
from wced.quantify.frp import compute_frp_emissions


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    load_factors.cache_clear()
    load_parameter_distributions.cache_clear()


def _event(total_frp_integral_mj: float | None = 1000.0) -> FireEvent:
    t0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    return FireEvent(
        facility_id=uuid4(),
        detected_at=t0,
        last_seen_at=t0 + timedelta(hours=6),
        peak_frp_mw=120.0,
        total_frp_integral_mj=total_frp_integral_mj,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.REPORTED,
        provenance_id=uuid4(),
        created_at=t0,
        updated_at=t0,
    )


# ---------------------------------------------------------------------------
# Hand-computed reference
# ---------------------------------------------------------------------------


def test_hand_computed_within_5pct() -> None:
    """Methodology §3.3 Eq. 2–4 at point values, I_raw=1000 MJ:

        I_raw × k_ext × d × α × (44/12) × f_C × r / 1000
      = 1000 × 1.0 × 0.70 × 0.368 × 3.6667 × 0.86 × 0.96 / 1000
      ≈ 0.780 tCO2e   (using d=mode=0.70)

    The MC p50 lands lower because d ~ Triangular(0.4, 0.7, 0.95) is
    right-skewed (mean 0.683, not 0.70). Since all six samples are
    independent, ``E[product] = product of E[·]`` exactly — so dist.mean
    is the right quantity to compare against a hand calculation that uses
    the per-factor means.
    """
    dist = compute_frp_emissions(_event(1000.0), load_factors(), rng_seed=42)
    expected_from_modes = (
        1000.0 * 1.0 * 0.70 * 0.368 * (44.0 / 12.0) * 0.86 * 0.96 / 1000.0
    )
    expected_from_means = (
        1000.0
        * 1.0
        * ((0.4 + 0.7 + 0.95) / 3.0)
        * 0.368
        * (44.0 / 12.0)
        * 0.86
        * ((0.92 + 0.96 + 0.98) / 3.0)
        / 1000.0
    )
    assert dist.units == "tCO2e"
    assert dist.methodology_version == "1.0"
    # Sanity check on the documented point-value calc.
    assert abs(expected_from_modes - 0.780) < 0.01
    # The actual MC mean should match product-of-means within MC noise.
    assert abs(dist.mean - expected_from_means) / expected_from_means < 0.05
    # And the p50 stays near both, within a looser band.
    assert abs(dist.p50 - expected_from_modes) / expected_from_modes < 0.10


# ---------------------------------------------------------------------------
# Property: doubling FRP roughly doubles p50
# ---------------------------------------------------------------------------


def test_doubling_frp_doubles_p50() -> None:
    factors = load_factors()
    d1 = compute_frp_emissions(_event(1000.0), factors, rng_seed=7)
    d2 = compute_frp_emissions(_event(2000.0), factors, rng_seed=7)
    ratio = d2.p50 / d1.p50
    assert 1.9 < ratio < 2.1


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_seed_reproducibility() -> None:
    factors = load_factors()
    event = _event(1000.0)
    a = compute_frp_emissions(event, factors, rng_seed=123)
    b = compute_frp_emissions(event, factors, rng_seed=123)
    assert a.samples is not None and b.samples is not None
    assert (a.samples == b.samples).all()
    assert a.provenance_id == b.provenance_id


def test_different_seeds_produce_different_samples() -> None:
    factors = load_factors()
    event = _event(1000.0)
    a = compute_frp_emissions(event, factors, rng_seed=1)
    b = compute_frp_emissions(event, factors, rng_seed=2)
    assert a.samples is not None and b.samples is not None
    assert not (a.samples == b.samples).all()


# ---------------------------------------------------------------------------
# Percentile invariant
# ---------------------------------------------------------------------------


def test_percentile_ordering() -> None:
    dist = compute_frp_emissions(_event(1500.0), load_factors(), rng_seed=42)
    assert dist.p5 <= dist.p50 <= dist.p95


def test_percentile_ordering_small_event() -> None:
    dist = compute_frp_emissions(_event(50.0), load_factors(), rng_seed=99)
    assert dist.p5 <= dist.p50 <= dist.p95


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_integral_raises() -> None:
    with pytest.raises(ValueError, match="total_frp_integral_mj"):
        compute_frp_emissions(_event(None), load_factors())


def test_non_positive_integral_raises() -> None:
    # Pydantic forbids negative on the model; zero must be caught here.
    with pytest.raises(ValueError, match="non-positive"):
        compute_frp_emissions(_event(0.0), load_factors())


def test_invalid_n_samples_raises() -> None:
    with pytest.raises(ValueError, match="n_samples"):
        compute_frp_emissions(_event(1000.0), load_factors(), n_samples=0)


def test_n_samples_respected() -> None:
    dist = compute_frp_emissions(
        _event(1000.0), load_factors(), n_samples=500, rng_seed=0
    )
    assert dist.samples is not None
    assert len(dist.samples) == 500


# ---------------------------------------------------------------------------
# Baseline subtraction (methodology v1.0.1 §3.3)
# ---------------------------------------------------------------------------


def _make_baseline(
    facility_id=None,
    baseline_frp_mw: float = 10.0,
    baseline_std_mw: float = 3.0,
    is_fallback: bool = False,
) -> FacilityBaseline:
    store = InMemoryProvenanceStore()
    fid = facility_id or uuid4()
    if is_fallback:
        return compute_baseline(fid, [], store=store, reference_time=datetime(2026, 3, 15, tzinfo=UTC))
    obs = [(datetime(2026, 3, 10, tzinfo=UTC), baseline_frp_mw)]
    return compute_baseline(fid, obs, store=store, reference_time=datetime(2026, 3, 15, tzinfo=UTC))


class TestBaselineSubtraction:
    def test_baseline_reduces_emissions(self) -> None:
        factors = load_factors()
        event = _event(1_000_000.0)
        baseline = _make_baseline(baseline_frp_mw=20.0)
        without = compute_frp_emissions(event, factors, rng_seed=42)
        with_bl = compute_frp_emissions(event, factors, rng_seed=42, baseline=baseline)
        assert with_bl.p50 < without.p50

    def test_methodology_version_bumped(self) -> None:
        baseline = _make_baseline()
        dist = compute_frp_emissions(_event(100_000.0), load_factors(), rng_seed=42, baseline=baseline)
        assert dist.methodology_version == "1.0.1"

    def test_no_baseline_keeps_v1_0(self) -> None:
        dist = compute_frp_emissions(_event(1000.0), load_factors(), rng_seed=42)
        assert dist.methodology_version == "1.0"

    def test_baseline_exceeds_event_gives_zero(self) -> None:
        event = _event(100.0)  # small event, 6 hours
        baseline = _make_baseline(baseline_frp_mw=500.0)  # huge baseline
        dist = compute_frp_emissions(event, load_factors(), rng_seed=42, baseline=baseline)
        assert dist.p50 == pytest.approx(0.0)
        assert dist.samples is not None
        assert np.all(dist.samples == 0.0)

    def test_fallback_baseline_flagged(self) -> None:
        event = _event(100_000.0)
        baseline = _make_baseline(is_fallback=True)
        dist = compute_frp_emissions(event, load_factors(), rng_seed=42, baseline=baseline)
        assert dist.methodology_version == "1.0.1"

    def test_non_fallback_baseline_not_flagged(self) -> None:
        event = _event(100_000.0)
        baseline = _make_baseline(baseline_frp_mw=5.0)
        dist = compute_frp_emissions(event, load_factors(), rng_seed=42, baseline=baseline)
        assert dist.methodology_version == "1.0.1"

    def test_net_frp_math(self) -> None:
        event = _event(100_000.0)  # 6 hours
        baseline = _make_baseline(baseline_frp_mw=10.0)  # 10 MW
        # baseline_mj_per_day = 10 * 86400 = 864000 MJ/day
        # duration = 6h = 0.25 days
        # baseline_total = 864000 * 0.25 = 216000 MJ
        # net = max(0, 100000 - 216000) = 0 (baseline exceeds raw)
        dist = compute_frp_emissions(event, load_factors(), rng_seed=42, baseline=baseline)
        assert dist.p50 == pytest.approx(0.0)
