"""Tests for wced.quantify.inventory.compute_inventory_emissions.

Hand-computed expected values come from methodology v1.0 §3.4 Eq. 5
applied at the mean of each input distribution. The Shahran worked
example matches the example in §6 of the methodology PDF.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from wced.models.event import DetectionSource, FireEvent
from wced.models.facility import Facility, FacilityType
from wced.models.provenance import ConfidenceLabel
from wced.quantify.factors import load_factors, load_parameter_distributions
from wced.quantify.inventory import compute_inventory_emissions


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    load_factors.cache_clear()
    load_parameter_distributions.cache_clear()


def _event() -> FireEvent:
    t0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    return FireEvent(
        facility_id=uuid4(),
        detected_at=t0,
        last_seen_at=t0 + timedelta(hours=6),
        peak_frp_mw=120.0,
        total_frp_integral_mj=1000.0,
        detection_source=DetectionSource.FIRMS_VIIRS,
        confidence_label=ConfidenceLabel.REPORTED,
        provenance_id=uuid4(),
        created_at=t0,
        updated_at=t0,
    )


def _facility(
    facility_type: FacilityType = FacilityType.OIL_DEPOT,
    capacity_barrels: float = 500_000.0,
    capacity_uncertainty_pct: float = 5.0,
) -> Facility:
    return Facility(
        name="Shahran Depot",
        facility_type=facility_type,
        geometry_wkt="POINT(51.3 35.8)",
        country="IRN",
        capacity_barrels=capacity_barrels,
        capacity_uncertainty_pct=capacity_uncertainty_pct,
        source_url="https://example.org/registry/shahran",
        added_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Shahran worked example (methodology §6)
# ---------------------------------------------------------------------------


class TestShahranWorkedExample:
    """C=500,000 bbl, φ=0.60 (uniform midpoint), ψ=0.40, EF=0.425.

    Point estimate: 500_000 × 0.60 × 0.40 × 0.425 = 51,000 tCO2e.

    All four inputs are independent so E[product] = product of E[·].
    Using a symmetric ψ triangular and a tight capacity prior, both the
    MC mean and the MC median land within 5% of 51,000.
    """

    def test_point_estimate_matches_methodology(self) -> None:
        # Sanity check on the documented hand calculation.
        expected = 500_000.0 * 0.60 * 0.40 * 0.425
        assert expected == pytest.approx(51_000.0)

    def test_mc_p50_within_5pct(self) -> None:
        dist = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=load_factors(),
            params=load_parameter_distributions(),
            rng_seed=42,
        )
        assert dist.units == "tCO2e"
        assert dist.methodology_version == "1.0"
        assert abs(dist.p50 - 51_000.0) / 51_000.0 < 0.05
        assert abs(dist.mean - 51_000.0) / 51_000.0 < 0.05

    def test_percentile_ordering(self) -> None:
        dist = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=load_factors(),
            params=load_parameter_distributions(),
            rng_seed=42,
        )
        assert dist.p5 <= dist.p50 <= dist.p95


# ---------------------------------------------------------------------------
# Facility-type validation (CLAUDE.md "Deferred Decisions")
# ---------------------------------------------------------------------------


class TestFacilityTypeValidation:
    def test_crude_factor_rejected_for_gas_processing(self) -> None:
        """Per methodology §3.4, applying a crude-oil factor to a
        gas-processing facility must raise, not silently produce wrong
        numbers."""
        gas_plant = _facility(facility_type=FacilityType.GAS_PROCESSING)
        with pytest.raises(ValueError, match="not applicable"):
            compute_inventory_emissions(
                event=_event(),
                facility=gas_plant,
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                rng_seed=0,
            )

    def test_crude_factor_accepted_for_refinery(self) -> None:
        refinery = _facility(facility_type=FacilityType.REFINERY)
        dist = compute_inventory_emissions(
            event=_event(),
            facility=refinery,
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=load_factors(),
            params=load_parameter_distributions(),
            rng_seed=0,
        )
        assert dist.p50 > 0

    def test_mixture_rejected_if_any_factor_inapplicable(self) -> None:
        """If any factor in product_mix is inapplicable, the whole call
        fails — not just the inapplicable component."""
        gas_plant = _facility(facility_type=FacilityType.GAS_PROCESSING)
        with pytest.raises(ValueError, match="not applicable"):
            compute_inventory_emissions(
                event=_event(),
                facility=gas_plant,
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                product_mix={
                    "crude_oil_combustion": 0.5,
                    "refined_product_combustion": 0.5,
                },
                rng_seed=0,
            )


# ---------------------------------------------------------------------------
# Property: doubling capacity ~doubles p50
# ---------------------------------------------------------------------------


class TestScalingProperties:
    def test_doubling_capacity_doubles_p50(self) -> None:
        factors = load_factors()
        params = load_parameter_distributions()
        a = compute_inventory_emissions(
            event=_event(),
            facility=_facility(capacity_barrels=500_000.0),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            rng_seed=7,
        )
        b = compute_inventory_emissions(
            event=_event(),
            facility=_facility(capacity_barrels=1_000_000.0),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            rng_seed=7,
        )
        ratio = b.p50 / a.p50
        assert 1.9 < ratio < 2.1


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


class TestReproducibility:
    def test_same_seed_identical_samples(self) -> None:
        factors = load_factors()
        params = load_parameter_distributions()
        kwargs = dict(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            rng_seed=123,
        )
        a = compute_inventory_emissions(**kwargs)
        b = compute_inventory_emissions(**kwargs)
        assert a.samples is not None and b.samples is not None
        assert (a.samples == b.samples).all()
        assert a.provenance_id == b.provenance_id

    def test_different_seeds_differ(self) -> None:
        factors = load_factors()
        params = load_parameter_distributions()
        a = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            rng_seed=1,
        )
        b = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            rng_seed=2,
        )
        assert a.samples is not None and b.samples is not None
        assert not (a.samples == b.samples).all()


# ---------------------------------------------------------------------------
# Product mix
# ---------------------------------------------------------------------------


class TestProductMix:
    def test_default_is_pure_crude(self) -> None:
        factors = load_factors()
        params = load_parameter_distributions()
        default = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            rng_seed=99,
        )
        explicit = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            product_mix={"crude_oil_combustion": 1.0},
            rng_seed=99,
        )
        assert default.samples is not None and explicit.samples is not None
        assert (default.samples == explicit.samples).all()

    def test_refined_only_uses_higher_ef(self) -> None:
        """Refined products (EF=0.430) yield slightly higher p50 than crude
        (EF=0.425) under otherwise identical inputs."""
        factors = load_factors()
        params = load_parameter_distributions()
        crude = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            product_mix={"crude_oil_combustion": 1.0},
            rng_seed=11,
        )
        refined = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            product_mix={"refined_product_combustion": 1.0},
            rng_seed=11,
        )
        # 0.430 / 0.425 ≈ 1.0118
        ratio = refined.mean / crude.mean
        assert 1.005 < ratio < 1.02

    def test_weights_renormalized(self) -> None:
        """Mix weights that don't sum to 1 are renormalized; the result
        must equal the same mix with normalized weights."""
        factors = load_factors()
        params = load_parameter_distributions()
        unnormalized = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            product_mix={
                "crude_oil_combustion": 7.0,
                "refined_product_combustion": 3.0,
            },
            rng_seed=5,
        )
        normalized = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=factors,
            params=params,
            product_mix={
                "crude_oil_combustion": 0.7,
                "refined_product_combustion": 0.3,
            },
            rng_seed=5,
        )
        assert unnormalized.samples is not None
        assert normalized.samples is not None
        assert (unnormalized.samples == normalized.samples).all()

    def test_empty_mix_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            compute_inventory_emissions(
                event=_event(),
                facility=_facility(),
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                product_mix={},
                rng_seed=0,
            )

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match="weights must be >= 0"):
            compute_inventory_emissions(
                event=_event(),
                facility=_facility(),
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                product_mix={"crude_oil_combustion": -1.0},
                rng_seed=0,
            )

    def test_zero_total_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match="sum to > 0"):
            compute_inventory_emissions(
                event=_event(),
                facility=_facility(),
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                product_mix={"crude_oil_combustion": 0.0},
                rng_seed=0,
            )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_capacity_raises(self) -> None:
        facility = Facility(
            name="Unknown",
            facility_type=FacilityType.OIL_DEPOT,
            geometry_wkt="POINT(51.3 35.8)",
            country="IRN",
            capacity_barrels=None,
            source_url="https://example.org",
            added_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="capacity_barrels"):
            compute_inventory_emissions(
                event=_event(),
                facility=facility,
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                rng_seed=0,
            )

    def test_zero_capacity_raises(self) -> None:
        facility = _facility(capacity_barrels=0.0)
        with pytest.raises(ValueError, match="capacity_barrels"):
            compute_inventory_emissions(
                event=_event(),
                facility=facility,
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                rng_seed=0,
            )

    def test_invalid_n_samples_raises(self) -> None:
        with pytest.raises(ValueError, match="n_samples"):
            compute_inventory_emissions(
                event=_event(),
                facility=_facility(),
                fraction_destroyed_pdf=(0.30, 0.40, 0.50),
                factors=load_factors(),
                params=load_parameter_distributions(),
                n_samples=0,
                rng_seed=0,
            )

    @pytest.mark.parametrize(
        "pdf",
        [
            (-0.1, 0.4, 0.5),  # low < 0
            (0.3, 0.4, 1.1),   # high > 1
            (0.5, 0.4, 0.6),   # low > mode
            (0.3, 0.6, 0.4),   # mode > high
        ],
    )
    def test_bad_fraction_destroyed_pdf_raises(
        self, pdf: tuple[float, float, float]
    ) -> None:
        with pytest.raises(ValueError, match="fraction_destroyed_pdf"):
            compute_inventory_emissions(
                event=_event(),
                facility=_facility(),
                fraction_destroyed_pdf=pdf,
                factors=load_factors(),
                params=load_parameter_distributions(),
                rng_seed=0,
            )

    def test_n_samples_respected(self) -> None:
        dist = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.30, 0.40, 0.50),
            factors=load_factors(),
            params=load_parameter_distributions(),
            n_samples=500,
            rng_seed=0,
        )
        assert dist.samples is not None
        assert len(dist.samples) == 500

    def test_degenerate_psi_handled(self) -> None:
        """psi with low==mode==high is a constant — must not crash
        numpy.random.triangular (which requires low < high)."""
        dist = compute_inventory_emissions(
            event=_event(),
            facility=_facility(),
            fraction_destroyed_pdf=(0.4, 0.4, 0.4),
            factors=load_factors(),
            params=load_parameter_distributions(),
            rng_seed=0,
        )
        assert dist.samples is not None
        assert dist.p50 > 0
