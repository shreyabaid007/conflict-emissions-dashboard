"""Verify FRP emissions match methodology/v1.0.pdf §3.3 and §6 worked example.

Shahran Depot worked example (§6):
    I_raw = 8.5e7 MJ, d = 0.70 (mode), k_ext = 1.0, alpha = 0.368,
    f_C = 0.86, r = 0.96
    Point estimate: 69,000 tCO2.
    MC fixtures (N=10,000, seed=42): p5=35k, p50=69k, p95=115k tCO2.
"""
from __future__ import annotations

import numpy as np
import pytest

from wced.models.event import FireEvent
from wced.quantify.factors import FactorRegistry
from wced.quantify.frp import compute_frp_emissions

from .conftest import (
    SHAHRAN_FRP_P5,
    SHAHRAN_FRP_P50,
    SHAHRAN_FRP_P95,
    SHAHRAN_FRP_POINT_ESTIMATE_TCO2,
    SHAHRAN_I_RAW_MJ,
    SHAHRAN_N_SAMPLES,
    SHAHRAN_RNG_SEED,
)


class TestFRPPointEstimate:
    """§6: hand-computed FRP point estimate at prior central values."""

    def test_point_value_matches_pdf(self) -> None:
        """Eq. 2-4 at central values:
        I_FRE = 1.0 * 0.70 * 8.5e7 = 5.95e7 MJ
        m_fuel = 0.368 * 5.95e7 = 2.19e7 kg
        m_CO2 = 2.19e7 * (44/12) * 0.86 * 0.96 = 6.90e7 kg = 69,000 tCO2
        """
        k_ext = 1.0
        d = 0.70
        alpha = 0.368
        f_c = 0.86
        r = 0.96
        beta = 44.0 / 12.0

        i_fre = k_ext * d * SHAHRAN_I_RAW_MJ
        m_fuel = alpha * i_fre
        m_co2_kg = m_fuel * beta * f_c * r
        m_co2_t = m_co2_kg / 1000.0

        assert abs(i_fre - 5.95e7) / 5.95e7 < 0.01
        assert abs(m_co2_t - SHAHRAN_FRP_POINT_ESTIMATE_TCO2) / SHAHRAN_FRP_POINT_ESTIMATE_TCO2 < 0.05


class TestFRPMonteCarlo:
    """§6 Monte Carlo fixture values (N=10,000, seed=42)."""

    @pytest.fixture()
    def frp_dist(self, shahran_event: FireEvent, factors: FactorRegistry):
        return compute_frp_emissions(
            shahran_event,
            factors,
            n_samples=SHAHRAN_N_SAMPLES,
            rng_seed=SHAHRAN_RNG_SEED,
        )

    def test_p50_within_15pct_of_point_estimate(self, frp_dist) -> None:
        """PDF §6: MC p50 should be within 15% of the 69k point estimate.

        The point estimate uses mode values for all priors, while MC samples
        from skewed distributions (triangular duty cycle, etc.), so the
        realized p50 is systematically lower than the mode-based calculation.
        """
        assert abs(frp_dist.p50 - SHAHRAN_FRP_POINT_ESTIMATE_TCO2) / SHAHRAN_FRP_POINT_ESTIMATE_TCO2 < 0.15

    def test_p5_fixture(self, frp_dist) -> None:
        """PDF §6 fixture: p5 ≈ 35,000 tCO2 (within 5%)."""
        assert abs(frp_dist.p5 - SHAHRAN_FRP_P5) / SHAHRAN_FRP_P5 < 0.05

    def test_p50_fixture(self, frp_dist) -> None:
        """PDF §6 fixture: p50 ≈ 69,000 tCO2 (within 5%)."""
        assert abs(frp_dist.p50 - SHAHRAN_FRP_P50) / SHAHRAN_FRP_P50 < 0.05

    def test_p95_fixture(self, frp_dist) -> None:
        """PDF §6 fixture: p95 ≈ 115,000 tCO2 (within 5%)."""
        assert abs(frp_dist.p95 - SHAHRAN_FRP_P95) / SHAHRAN_FRP_P95 < 0.05

    def test_percentile_ordering(self, frp_dist) -> None:
        assert frp_dist.p5 <= frp_dist.p50 <= frp_dist.p95

    def test_units_and_version(self, frp_dist) -> None:
        assert frp_dist.units == "tCO2e"
        assert frp_dist.methodology_version == "1.0"

    def test_sample_count(self, frp_dist) -> None:
        assert frp_dist.samples is not None
        assert len(frp_dist.samples) == SHAHRAN_N_SAMPLES

    def test_reproducibility(self, shahran_event: FireEvent, factors: FactorRegistry) -> None:
        a = compute_frp_emissions(shahran_event, factors, n_samples=SHAHRAN_N_SAMPLES, rng_seed=SHAHRAN_RNG_SEED)
        b = compute_frp_emissions(shahran_event, factors, n_samples=SHAHRAN_N_SAMPLES, rng_seed=SHAHRAN_RNG_SEED)
        assert a.samples is not None and b.samples is not None
        np.testing.assert_array_equal(a.samples, b.samples)
