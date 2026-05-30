"""Verify inventory emissions match methodology/v1.0.pdf §3.4 and §6 worked example.

Shahran Depot worked example (§6):
    C = 500,000 bbl, phi = 0.60 (uniform midpoint), psi = 0.40,
    EF = 0.425 tCO2/barrel
    Point estimate: 51,000 tCO2.
    MC fixtures (N=10,000, seed=42): p5=24k, p50=51k, p95=92k tCO2.
"""
from __future__ import annotations

import numpy as np
import pytest

from wced.models.event import FireEvent
from wced.models.facility import Facility
from wced.quantify.factors import FactorRegistry
from wced.quantify.inventory import compute_inventory_emissions

from .conftest import (
    SHAHRAN_CAPACITY_BARRELS,
    SHAHRAN_FRACTION_DESTROYED_PDF,
    SHAHRAN_INV_P5,
    SHAHRAN_INV_P50,
    SHAHRAN_INV_P95,
    SHAHRAN_INVENTORY_POINT_ESTIMATE_TCO2,
    SHAHRAN_N_SAMPLES,
    SHAHRAN_RNG_SEED,
)


class TestInventoryPointEstimate:
    """§6: hand-computed inventory point estimate at prior central values."""

    def test_point_value_matches_pdf(self) -> None:
        """Eq. 5 at central values:
        m_CO2 = C * phi * psi * EF
              = 500,000 * 0.60 * 0.40 * 0.425
              = 51,000 tCO2
        """
        c = SHAHRAN_CAPACITY_BARRELS
        phi = 0.60
        psi = 0.40
        ef = 0.425

        m_co2 = c * phi * psi * ef
        assert abs(m_co2 - SHAHRAN_INVENTORY_POINT_ESTIMATE_TCO2) / SHAHRAN_INVENTORY_POINT_ESTIMATE_TCO2 < 0.01


class TestInventoryMonteCarlo:
    """§6 Monte Carlo fixture values (N=10,000, seed=42)."""

    @pytest.fixture()
    def inv_dist(
        self,
        shahran_event: FireEvent,
        shahran_facility: Facility,
        factors: FactorRegistry,
        params: FactorRegistry,
    ):
        return compute_inventory_emissions(
            event=shahran_event,
            facility=shahran_facility,
            fraction_destroyed_pdf=SHAHRAN_FRACTION_DESTROYED_PDF,
            factors=factors,
            params=params,
            n_samples=SHAHRAN_N_SAMPLES,
            rng_seed=SHAHRAN_RNG_SEED,
        )

    def test_p50_within_15pct_of_point_estimate(self, inv_dist) -> None:
        """PDF §6: MC p50 should be within 15% of the 51k point estimate.

        The point estimate uses midpoint values for all priors, while MC
        samples from the full distributions (uniform phi, triangular psi),
        so systematic deviation from the midpoint-based calculation is expected.
        """
        assert abs(inv_dist.p50 - SHAHRAN_INVENTORY_POINT_ESTIMATE_TCO2) / SHAHRAN_INVENTORY_POINT_ESTIMATE_TCO2 < 0.15

    def test_p5_fixture(self, inv_dist) -> None:
        """PDF §6 fixture: p5 ≈ 24,000 tCO2 (within 5%)."""
        assert abs(inv_dist.p5 - SHAHRAN_INV_P5) / SHAHRAN_INV_P5 < 0.05

    def test_p50_fixture(self, inv_dist) -> None:
        """PDF §6 fixture: p50 ≈ 51,000 tCO2 (within 5%)."""
        assert abs(inv_dist.p50 - SHAHRAN_INV_P50) / SHAHRAN_INV_P50 < 0.05

    def test_p95_fixture(self, inv_dist) -> None:
        """PDF §6 fixture: p95 ≈ 92,000 tCO2 (within 5%)."""
        assert abs(inv_dist.p95 - SHAHRAN_INV_P95) / SHAHRAN_INV_P95 < 0.05

    def test_percentile_ordering(self, inv_dist) -> None:
        assert inv_dist.p5 <= inv_dist.p50 <= inv_dist.p95

    def test_units_and_version(self, inv_dist) -> None:
        assert inv_dist.units == "tCO2e"
        assert inv_dist.methodology_version == "1.0"

    def test_sample_count(self, inv_dist) -> None:
        assert inv_dist.samples is not None
        assert len(inv_dist.samples) == SHAHRAN_N_SAMPLES

    def test_reproducibility(
        self,
        shahran_event: FireEvent,
        shahran_facility: Facility,
        factors: FactorRegistry,
        params: FactorRegistry,
    ) -> None:
        kwargs = dict(
            event=shahran_event,
            facility=shahran_facility,
            fraction_destroyed_pdf=SHAHRAN_FRACTION_DESTROYED_PDF,
            factors=factors,
            params=params,
            n_samples=SHAHRAN_N_SAMPLES,
            rng_seed=SHAHRAN_RNG_SEED,
        )
        a = compute_inventory_emissions(**kwargs)
        b = compute_inventory_emissions(**kwargs)
        assert a.samples is not None and b.samples is not None
        np.testing.assert_array_equal(a.samples, b.samples)
