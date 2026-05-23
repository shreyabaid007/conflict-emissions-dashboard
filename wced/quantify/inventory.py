"""Inventory-based combustion CO2 estimation.

Implements the inventory method from methodology/v1.0.pdf §3.4 (Eq. 5).
Where the FRP method (``wced.quantify.frp``) infers combusted mass from
satellite-observed radiant energy, the inventory method estimates the
upper-bound emission as the fraction of a facility's nameplate fuel
inventory that visual assessment judges destroyed:

    m_CO2 = C × φ × ψ × EF                             (§3.4 Eq. 5)

where

    C  = facility nameplate capacity (barrels)
    φ  = fraction of capacity present at strike (facility_inventory_at_strike)
    ψ  = fraction of present inventory destroyed (visual assessment)
    EF = emission factor (tCO2 per barrel)

Each of the four inputs is sampled (n=10,000 by default) so the result is
a full :class:`Distribution` with provenance and uncertainty bounds —
never a point estimate (CLAUDE.md "Uncertainty is mandatory").

Per CLAUDE.md "Deferred Decisions" and methodology §3.4, the chosen
emission factor must declare the event's facility type in its
``applicable_facility_types`` list. Applying a crude-oil factor to a
gas-processing facility silently produces wrong numbers — this module
raises :class:`ValueError` instead.
"""
from __future__ import annotations

import uuid
from uuid import UUID

import numpy as np

from wced.models.event import FireEvent
from wced.models.facility import Facility
from wced.quantify.distribution import Distribution
from wced.quantify.factors import EmissionFactor, FactorRegistry

__all__ = ["compute_inventory_emissions"]


# Provenance namespace for inventory-derived estimates. Stable across runs
# so the same inputs always yield the same derived provenance ID.
_INVENTORY_PROV_NS = uuid.UUID("c2e3f4a5-0000-5000-8000-000000000003")

# Parameter key for the fraction of capacity present at strike
# (parameter_distributions.yaml). Default uniform(0.3, 0.9).
_INVENTORY_AT_STRIKE_KEY = "facility_inventory_at_strike"


def _validate_facility_type(
    factor_key: str, factor: EmissionFactor, facility: Facility
) -> None:
    """Raise ValueError if ``factor`` cannot legitimately be applied to ``facility``.

    Per CLAUDE.md "Deferred Decisions" and methodology §3.4: every emission
    factor used for inventory accounting must declare which
    :class:`FacilityType` values it is calibrated for. A factor with no
    ``applicable_facility_types`` list is rejected (silent matching is
    forbidden); a factor that lists the wrong types is rejected with a
    descriptive error.
    """
    allowed = factor.applicable_facility_types
    if not allowed:
        raise ValueError(
            f"emission factor {factor_key!r} has no applicable_facility_types "
            f"declared in emission_factors.yaml; cannot validate against "
            f"facility {facility.id} ({facility.facility_type.value}). "
            "Per methodology §3.4, every inventory-method factor must "
            "declare its facility-type binding."
        )
    if facility.facility_type.value not in allowed:
        raise ValueError(
            f"emission factor {factor_key!r} is not applicable to facility "
            f"{facility.id} of type {facility.facility_type.value}; "
            f"applicable_facility_types={allowed}. Per methodology §3.4, "
            "applying a mismatched factor (e.g. crude-oil combustion to a "
            "gas-processing plant) silently produces wrong numbers."
        )


def _sample_factor(
    factor: EmissionFactor, n_samples: int, rng: np.random.Generator
) -> np.ndarray:
    """Draw ``n_samples`` from an EmissionFactor's distribution.

    Mirrors :meth:`EmissionFactor.sample` but returns the raw array so we
    can stack and mix samples from multiple factors without constructing
    an intermediate Distribution (which would compute percentiles we throw
    away).
    """
    if factor.distribution == "triangular":
        assert factor.low is not None and factor.mode is not None and factor.high is not None
        return rng.triangular(factor.low, factor.mode, factor.high, n_samples)
    if factor.distribution == "normal":
        assert factor.sigma is not None
        return rng.normal(factor.value, factor.sigma, n_samples)
    if factor.distribution == "uniform":
        assert factor.low is not None and factor.high is not None
        return rng.uniform(factor.low, factor.high, n_samples)
    # constant
    return np.full(n_samples, factor.value, dtype=float)


def compute_inventory_emissions(
    event: FireEvent,
    facility: Facility,
    fraction_destroyed_pdf: tuple[float, float, float],
    factors: FactorRegistry,
    params: FactorRegistry,
    n_samples: int = 10_000,
    rng_seed: int | None = None,
    product_mix: dict[str, float] | None = None,
    methodology_version: str = "1.0",
) -> Distribution:
    """Estimate combustion CO2 from a facility's inventory loss.

    Implements methodology/v1.0.pdf §3.4 Eq. 5. Returns a
    :class:`Distribution` in tCO2e with ``methodology_version="1.0"`` and a
    deterministic ``provenance_id`` derived from the inputs.

    Parameters
    ----------
    event : FireEvent
        The fire event attached to ``facility``. Its provenance and id
        flow into the derived ``provenance_id`` so the audit trail can
        reach back to the upstream detection.
    facility : Facility
        Struck facility. ``capacity_barrels`` must be populated;
        ``capacity_uncertainty_pct`` defines the 1-σ symmetric spread on
        ``C`` (truncated at zero — negative capacity is non-physical).
    fraction_destroyed_pdf : tuple[float, float, float]
        ``(low, mode, high)`` triangular parameters for ψ, the fraction
        of present inventory destroyed by the strike. Provided by the
        visual-assessment step (Sentinel-2 / Pleiades / Maxar editorial
        review). Must satisfy ``0 <= low <= mode <= high <= 1``. Samples
        are clipped to ``[0, 1]`` defensively.
    factors : FactorRegistry
        Loaded ``data/emission_factors.yaml``. Must contain every key in
        ``product_mix`` (or ``crude_oil_combustion`` for the default).
    params : FactorRegistry
        Loaded ``data/parameter_distributions.yaml``. Must contain
        ``facility_inventory_at_strike``.
    n_samples : int, default 10_000
        Monte Carlo draw count. Methodology §3.4 fixes the operational
        default at 10,000; lower values are permitted for tests.
    rng_seed : int or None
        Seed for ``numpy.random.default_rng``. When provided, samples are
        reproducible across runs; the seed should be persisted on the
        upstream ProvenanceRecord.
    product_mix : dict[str, float] or None
        Weighted mix of emission factor keys, e.g.
        ``{"crude_oil_combustion": 0.7, "refined_product_combustion": 0.3}``.
        Per-sample EF is the weighted sum of independent samples from
        each named factor. Weights must be non-negative and sum to a
        positive number (they are renormalized to sum to 1). Default:
        100% ``crude_oil_combustion``.

    Returns
    -------
    Distribution
        Tonnes of CO2-equivalent with p5/p50/p95 percentiles, mean, std,
        and the full sample array for downstream arithmetic.

    Raises
    ------
    ValueError
        If ``facility.capacity_barrels`` is None or non-positive, if
        ``fraction_destroyed_pdf`` is malformed or out of [0, 1], if
        ``n_samples < 1``, if ``product_mix`` is empty or has non-positive
        total weight, or if any selected emission factor is not
        applicable to ``facility.facility_type``.
    KeyError
        If a required factor or parameter key is missing.
    """
    if n_samples < 1:
        raise ValueError(f"n_samples must be >= 1; got {n_samples}")
    if facility.capacity_barrels is None or facility.capacity_barrels <= 0:
        raise ValueError(
            f"facility {facility.id} has no positive capacity_barrels "
            f"(got {facility.capacity_barrels}); inventory method requires "
            "a nameplate capacity (methodology §3.4)."
        )
    low, mode, high = fraction_destroyed_pdf
    if not (0.0 <= low <= mode <= high <= 1.0):
        raise ValueError(
            f"fraction_destroyed_pdf must satisfy 0 <= low <= mode <= high <= 1; "
            f"got (low={low}, mode={mode}, high={high})"
        )

    mix = product_mix if product_mix is not None else {"crude_oil_combustion": 1.0}
    if not mix:
        raise ValueError("product_mix must be non-empty")
    if any(w < 0 for w in mix.values()):
        raise ValueError(f"product_mix weights must be >= 0; got {mix}")
    total_weight = sum(mix.values())
    if total_weight <= 0:
        raise ValueError(f"product_mix weights must sum to > 0; got {mix}")

    # Validate every selected factor against the facility type BEFORE
    # drawing any samples — fail fast on misconfiguration.
    selected: list[tuple[str, EmissionFactor, float]] = []
    for key, weight in mix.items():
        factor = factors[key]
        _validate_facility_type(key, factor, facility)
        selected.append((key, factor, weight / total_weight))

    phi_param = params[_INVENTORY_AT_STRIKE_KEY]

    rng = np.random.default_rng(rng_seed)

    # C ~ Normal(capacity, (pct/100) * capacity), truncated at 0. Negative
    # capacity is non-physical; clipping (rather than rejection-resampling)
    # is consistent with the symmetric-σ convention in the facility model.
    capacity = float(facility.capacity_barrels)
    capacity_sigma = (facility.capacity_uncertainty_pct / 100.0) * capacity
    capacity_samples = rng.normal(capacity, capacity_sigma, n_samples)
    np.clip(capacity_samples, 0.0, None, out=capacity_samples)

    # φ ~ from parameter_distributions.yaml (uniform(0.3, 0.9) in v1.0).
    phi_samples = _sample_factor(phi_param, n_samples, rng)

    # ψ ~ Triangular(low, mode, high), clipped to [0, 1] defensively.
    # numpy.random.triangular requires low < high; the validator above
    # admits low == mode == high, which we handle as a constant.
    if low == high:
        psi_samples = np.full(n_samples, mode, dtype=float)
    else:
        psi_samples = rng.triangular(low, mode, high, n_samples)
    np.clip(psi_samples, 0.0, 1.0, out=psi_samples)

    # EF ~ weighted mixture of factor samples. Each sub-factor is drawn
    # independently per iteration; per-sample EF = Σ w_i * EF_i. This
    # propagates each factor's distribution into the combined estimate
    # without collapsing them to means.
    ef_samples = np.zeros(n_samples, dtype=float)
    for _key, factor, weight in selected:
        ef_samples += weight * _sample_factor(factor, n_samples, rng)

    # m_CO2 = C × φ × ψ × EF  (§3.4 Eq. 5). EF is in tCO2/barrel and C
    # in barrels, so the product is already in tCO2 — no unit conversion
    # at the output boundary.
    co2_tonnes_samples = capacity_samples * phi_samples * psi_samples * ef_samples

    mix_tag = ",".join(f"{k}={w:.6f}" for k, w in sorted(mix.items()))
    provenance_id = uuid.uuid5(
        _INVENTORY_PROV_NS,
        f"{event.provenance_id}|{facility.id}|{_INVENTORY_AT_STRIKE_KEY}|"
        f"mix=[{mix_tag}]|psi=({low},{mode},{high})|"
        f"n={n_samples}|seed={rng_seed}",
    )

    return Distribution.from_samples(
        co2_tonnes_samples,
        units="tCO2e",
        methodology_version=methodology_version,
        provenance_id=provenance_id,
    )
