"""FRP-based combustion CO2 estimation.

Implements the Fire Radiative Power (FRP) method from methodology/v1.0.pdf
§3.3. Given a persistent FireEvent with a raw time-integrated FRP measurement
(``total_frp_integral_mj`` = I_raw, the trapezoidal integral over FIRMS
overpasses), subtracts the facility's baseline FRP (routine flaring), corrects
for revisit gaps and burn duty cycle, then converts radiant energy → fuel mass
→ CO2 via Monte Carlo over the published priors.

Baseline subtraction (§3.3, methodology v1.0.1):

    I_net = max(0, I_raw - baseline_frp_mj_per_day × duration_days)

The baseline is the facility's 75th-percentile background FRP (see
``wced.detect.baseline``). Events within 30 days of the war start date
(2026-02-28) may lack sufficient history; these use a fallback baseline
(0 MW mean, 50 MW std) and are flagged with "insufficient_baseline_history".

Equation chain (§3.3, Eq. 2 and following):

    I_FRE   = k_ext × d × I_net                    # gap-corrected FRE (MJ)
    m_fuel  = α × I_FRE                            # combusted fuel (kg)
    m_CO2   = m_fuel × (44/12) × f_C × r           # CO2 mass (kg)
    tCO2e   = m_CO2 / 1000

where 44/12 is the molecular weight ratio CO2/C and f_C is the carbon mass
fraction of hydrocarbon fuel (methodology §3.3, following Wooster et al.
2005 and Hobbs & Radke 1992).

Uncertainty propagation (§3.3, n=10,000 by default):

- ``frp_extrapolation_factor`` (k_ext) ~ Normal(1.0, 0.15)
  (``data/parameter_distributions.yaml``)
- ``burn_duty_cycle`` (d) ~ Triangular(0.4, 0.7, 0.95)
  (``data/parameter_distributions.yaml``)
- ``frp_to_combustion_rate`` (α) ~ Normal(0.368, 0.05) kg/MJ
  (Wooster et al. 2005; ``data/emission_factors.yaml``)
- ``carbon_recovery_as_co2`` (r) ~ Triangular(0.92, 0.96, 0.98)
  (Hobbs & Radke 1992; ``data/emission_factors.yaml``)
- ``I_net`` ~ Normal(measured, 0.2 × measured) MJ
  — captures residual sampling noise in the trapezoidal integral (§3.3.4)
"""
from __future__ import annotations

import logging
import uuid
from uuid import UUID

import numpy as np

from wced.detect.baseline import FacilityBaseline
from wced.models.event import FireEvent
from wced.quantify.distribution import Distribution
from wced.quantify.factors import FactorRegistry, load_parameter_distributions

__all__ = ["compute_frp_emissions"]

log = logging.getLogger(__name__)

# Molecular weight ratio CO2 / C = 44 / 12. Methodology v1.0 §3.3.
_CO2_PER_C = 44.0 / 12.0

# Carbon mass fraction of hydrocarbon fuel (methodology §3.3, Eq. 2 — f_C).
# Bulk hydrocarbon ≈ 86% carbon by mass; used as a point value pending a
# fuel-mix prior in a future methodology version.
_CARBON_MASS_FRACTION = 0.86

# Provenance namespace for FRP-derived estimates. Stable across runs so the
# same (event, factors, seed) triple yields the same derived provenance ID.
_FRP_PROV_NS = uuid.UUID("c2e3f4a5-0000-5000-8000-000000000002")

# Residual sampling-noise σ as fraction of I_raw (methodology §3.3.4).
_FRP_INTEGRAL_REL_SIGMA = 0.20

_FRP_RATE_KEY = "frp_to_combustion_rate"
_CARBON_RECOVERY_KEY = "carbon_recovery_as_co2"
_BURN_DUTY_CYCLE_KEY = "burn_duty_cycle"
_FRP_EXTRAPOLATION_KEY = "frp_extrapolation_factor"


def compute_frp_emissions(
    event: FireEvent,
    factors: FactorRegistry,
    n_samples: int = 10_000,
    rng_seed: int | None = None,
    baseline: FacilityBaseline | None = None,
    methodology_version: str | None = None,
) -> Distribution:
    """Estimate combustion CO2 from a FireEvent's integrated FRP.

    Implements methodology/v1.0.pdf §3.3 (FRP method). Subtracts the
    facility's baseline FRP before Monte Carlo sampling to avoid attributing
    routine flaring to the conflict event.

    Parameters
    ----------
    event : FireEvent
        Persistent fire event. ``total_frp_integral_mj`` must be populated
        (None means the event has only one overpass; methodology §3.2
        forbids quantifying single-overpass detections).
    factors : FactorRegistry
        Loaded ``data/emission_factors.yaml``. Must contain
        ``frp_to_combustion_rate`` and ``carbon_recovery_as_co2``.
    n_samples : int, default 10_000
        Number of Monte Carlo draws. Methodology §3.3 fixes the operational
        default at 10,000; callers may lower it for tests.
    rng_seed : int or None
        Seed for ``numpy.random.default_rng``. When provided, samples are
        reproducible. Store the seed in the upstream ProvenanceRecord.
    baseline : FacilityBaseline or None
        Pre-computed facility baseline. When None, no baseline subtraction
        is performed (backward-compatible with pre-v1.0.1 callers).

    Returns
    -------
    Distribution
        Tonnes of CO2-equivalent with p5/p50/p95 percentiles and the full
        sample array attached for downstream arithmetic.

    Raises
    ------
    ValueError
        If ``event.total_frp_integral_mj`` is None or non-positive, or if
        ``n_samples < 1``.
    KeyError
        If a required factor key is missing from ``factors``.
    """
    if n_samples < 1:
        raise ValueError(f"n_samples must be >= 1; got {n_samples}")
    measured = event.total_frp_integral_mj
    if measured is None:
        raise ValueError(
            f"event {event.id} has no total_frp_integral_mj; "
            "methodology v1.0 §3.2 requires a persistent (≥2 overpass) "
            "event before FRP quantification"
        )
    if measured <= 0:
        raise ValueError(
            f"event {event.id} has non-positive total_frp_integral_mj={measured}"
        )

    # --- Baseline subtraction (methodology v1.0.1 §3.3) ---
    _auto_version = "1.0"
    provenance_notes: str | None = None
    net_frp_mj = measured

    if baseline is not None:
        _auto_version = "1.0.1"
        duration_days = event.duration_hours / 24.0
        # baseline_frp_mw is in MW; convert to MJ/day: MW × 86400 s/day = MJ/day
        baseline_mj_per_day = baseline.baseline_frp_mw * 86400.0
        baseline_total_mj = baseline_mj_per_day * duration_days
        net_frp_mj = max(0.0, measured - baseline_total_mj)

        if baseline.is_fallback:
            provenance_notes = "insufficient_baseline_history"

        log.info(
            "baseline subtraction: event %s raw=%.1f MJ, baseline=%.1f MJ "
            "(%.2f MW × %.1f days), net=%.1f MJ%s",
            event.id,
            measured,
            baseline_total_mj,
            baseline.baseline_frp_mw,
            duration_days,
            net_frp_mj,
            f" [{provenance_notes}]" if provenance_notes else "",
        )

        if net_frp_mj == 0.0:
            provenance_id = uuid.uuid5(
                _FRP_PROV_NS,
                f"{event.provenance_id}|baseline_zeroed|{baseline.provenance_id}",
            )
            _ver = methodology_version if methodology_version is not None else _auto_version
            return Distribution.from_samples(
                np.zeros(n_samples),
                units="tCO2e",
                methodology_version=_ver,
                provenance_id=provenance_id,
            )

    _resolved_version = methodology_version if methodology_version is not None else _auto_version

    rate_factor = factors[_FRP_RATE_KEY]
    recovery_factor = factors[_CARBON_RECOVERY_KEY]
    parameters = load_parameter_distributions()
    duty_param = parameters[_BURN_DUTY_CYCLE_KEY]
    kext_param = parameters[_FRP_EXTRAPOLATION_KEY]

    rng = np.random.default_rng(rng_seed)

    # I_net sampling — residual noise on the trapezoidal integral (§3.3.4).
    i_net_samples = rng.normal(
        net_frp_mj, _FRP_INTEGRAL_REL_SIGMA * net_frp_mj, n_samples
    )
    # Gap-correction multipliers (methodology §3.3 Eq. 2).
    kext_samples = rng.normal(kext_param.value, kext_param.sigma, n_samples)  # type: ignore[arg-type]
    duty_samples = rng.triangular(
        duty_param.low,  # type: ignore[arg-type]
        duty_param.mode,  # type: ignore[arg-type]
        duty_param.high,  # type: ignore[arg-type]
        n_samples,
    )
    # Emission-factor priors (§3.3.2, §3.3.3).
    rate_samples = rng.normal(rate_factor.value, rate_factor.sigma, n_samples)  # type: ignore[arg-type]
    recovery_samples = rng.triangular(
        recovery_factor.low,  # type: ignore[arg-type]
        recovery_factor.mode,  # type: ignore[arg-type]
        recovery_factor.high,  # type: ignore[arg-type]
        n_samples,
    )

    # I_FRE = k_ext * d * I_net                     (Eq. 2)
    # m_fuel = α * I_FRE                            (Eq. 3)
    # m_CO2  = m_fuel * (44/12) * f_C * r           (Eq. 4)
    # tCO2e  = m_CO2 / 1000 (convert at output boundary only).
    i_fre_samples = kext_samples * duty_samples * i_net_samples
    co2_tonnes_samples = (
        rate_samples
        * i_fre_samples
        * _CO2_PER_C
        * _CARBON_MASS_FRACTION
        * recovery_samples
        / 1000.0
    )

    baseline_prov = f"|baseline={baseline.provenance_id}" if baseline else ""
    provenance_id = uuid.uuid5(
        _FRP_PROV_NS,
        f"{event.provenance_id}|{_FRP_EXTRAPOLATION_KEY}|{_BURN_DUTY_CYCLE_KEY}|"
        f"{_FRP_RATE_KEY}|{_CARBON_RECOVERY_KEY}|n={n_samples}|seed={rng_seed}"
        f"{baseline_prov}",
    )

    return Distribution.from_samples(
        co2_tonnes_samples,
        units="tCO2e",
        methodology_version=_resolved_version,
        provenance_id=provenance_id,
    )
