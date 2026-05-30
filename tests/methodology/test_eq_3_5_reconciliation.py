"""Verify reconciliation matches methodology/v1.0.pdf §3.5 and §6 worked example.

Shahran Depot worked example (§6):
    rho = 51,000 / 69,000 ≈ 0.74, inside [0.5, 2.0] → reconciled_ok=True.
    Envelope p50 between 51k and 69k tCO2.
"""
from __future__ import annotations

import numpy as np
import pytest

from wced.models.event import FireEvent
from wced.models.facility import Facility
from wced.quantify.distribution import Distribution
from wced.quantify.factors import FactorRegistry
from wced.quantify.frp import compute_frp_emissions
from wced.quantify.inventory import compute_inventory_emissions
from wced.quantify.reconcile import reconcile_estimates

from .conftest import (
    SHAHRAN_FRACTION_DESTROYED_PDF,
    SHAHRAN_FRP_P50,
    SHAHRAN_INV_P50,
    SHAHRAN_N_SAMPLES,
    SHAHRAN_RNG_SEED,
)


@pytest.fixture()
def frp_dist(shahran_event: FireEvent, factors: FactorRegistry) -> Distribution:
    return compute_frp_emissions(
        shahran_event, factors, n_samples=SHAHRAN_N_SAMPLES, rng_seed=SHAHRAN_RNG_SEED,
    )


@pytest.fixture()
def inv_dist(
    shahran_event: FireEvent,
    shahran_facility: Facility,
    factors: FactorRegistry,
    params: FactorRegistry,
) -> Distribution:
    return compute_inventory_emissions(
        event=shahran_event,
        facility=shahran_facility,
        fraction_destroyed_pdf=SHAHRAN_FRACTION_DESTROYED_PDF,
        factors=factors,
        params=params,
        n_samples=SHAHRAN_N_SAMPLES,
        rng_seed=SHAHRAN_RNG_SEED,
    )


class TestReconciliationShahran:
    """§6: Shahran reconciliation — rho ≈ 0.74, reconciled_ok=True."""

    def test_agreement_ratio(
        self,
        shahran_event: FireEvent,
        frp_dist: Distribution,
        inv_dist: Distribution,
    ) -> None:
        """PDF §6: rho = 51k/69k ≈ 0.74."""
        result = reconcile_estimates(shahran_event, frp_dist, inv_dist, None)
        assert result.agreement_ratio is not None
        assert abs(result.agreement_ratio - 0.74) < 0.10

    def test_reconciled_ok(
        self,
        shahran_event: FireEvent,
        frp_dist: Distribution,
        inv_dist: Distribution,
    ) -> None:
        result = reconcile_estimates(shahran_event, frp_dist, inv_dist, None)
        assert result.reconciled_ok is True
        assert result.needs_review is False

    def test_envelope_p50_between_methods(
        self,
        shahran_event: FireEvent,
        frp_dist: Distribution,
        inv_dist: Distribution,
    ) -> None:
        """PDF §6: envelope p50 should fall between the two method p50s."""
        result = reconcile_estimates(shahran_event, frp_dist, inv_dist, None)
        assert result.final_distribution is not None
        low = min(frp_dist.p50, inv_dist.p50)
        high = max(frp_dist.p50, inv_dist.p50)
        assert low <= result.final_distribution.p50 <= high

    def test_envelope_is_pooled_samples(
        self,
        shahran_event: FireEvent,
        frp_dist: Distribution,
        inv_dist: Distribution,
    ) -> None:
        """§3.5: envelope = union of both MC sample arrays."""
        result = reconcile_estimates(shahran_event, frp_dist, inv_dist, None)
        assert result.final_distribution is not None
        assert result.final_distribution.samples is not None
        expected_n = SHAHRAN_N_SAMPLES * 2
        assert len(result.final_distribution.samples) == expected_n

    def test_methodology_section(
        self,
        shahran_event: FireEvent,
        frp_dist: Distribution,
        inv_dist: Distribution,
    ) -> None:
        result = reconcile_estimates(shahran_event, frp_dist, inv_dist, None)
        assert result.methodology_section == "3.5"


class TestReconciliationBoundary:
    """§3.5: agreement band [0.5, 2.0] boundary cases."""

    def _make_dist(self, p50: float) -> Distribution:
        from uuid import uuid4
        samples = np.full(1000, p50)
        return Distribution.from_samples(
            samples, units="tCO2e", methodology_version="1.0", provenance_id=uuid4(),
        )

    def test_disagreement_below(self, shahran_event: FireEvent) -> None:
        """rho < 0.5 → needs_review."""
        frp = self._make_dist(100.0)
        inv = self._make_dist(40.0)
        result = reconcile_estimates(shahran_event, frp, inv, None)
        assert result.needs_review is True
        assert result.reconciled_ok is False
        assert result.final_distribution is None

    def test_disagreement_above(self, shahran_event: FireEvent) -> None:
        """rho > 2.0 → needs_review."""
        frp = self._make_dist(100.0)
        inv = self._make_dist(250.0)
        result = reconcile_estimates(shahran_event, frp, inv, None)
        assert result.needs_review is True
        assert result.reconciled_ok is False

    def test_near_boundary_low(self, shahran_event: FireEvent) -> None:
        """rho ∈ [0.50, 0.55] → reconciled_ok but near_boundary flagged."""
        frp = self._make_dist(100.0)
        inv = self._make_dist(52.0)
        result = reconcile_estimates(shahran_event, frp, inv, None)
        assert result.reconciled_ok is True
        assert result.near_boundary is True

    def test_near_boundary_high(self, shahran_event: FireEvent) -> None:
        """rho ∈ [1.82, 2.00] → reconciled_ok but near_boundary flagged."""
        frp = self._make_dist(100.0)
        inv = self._make_dist(190.0)
        result = reconcile_estimates(shahran_event, frp, inv, None)
        assert result.reconciled_ok is True
        assert result.near_boundary is True

    def test_reported_never_in_headline(self, shahran_event: FireEvent) -> None:
        """§3.5: reported estimate stored as CLAIMED, never in final."""
        frp = self._make_dist(100.0)
        reported = self._make_dist(80.0)
        result = reconcile_estimates(shahran_event, frp, None, reported)
        assert result.final_distribution is frp
        assert result.reported_estimate is reported
