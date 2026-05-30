"""Rolling FRP baseline computation for registered Facilities.

Each facility has a characteristic background Fire Radiative Power (FRP) from
routine industrial flaring. Refineries and gas-processing plants flare
continuously; attributing that routine FRP to a conflict event would overstate
emissions. This module computes the per-facility rolling 30-day 75th-percentile
FRP and subtracts it from candidate FRP so only the excess is attributed.

Design notes
------------
- The baseline window excludes any FRP observations that fall within a known
  active-event window, preventing a large fire from inflating the "normal"
  background.
- We use the 75th percentile (not median) because flaring is intermittent and
  the median underestimates the characteristic background for active refineries.
- Baseline uncertainty uses IQR/1.349 as a robust estimator of standard
  deviation, resistant to outliers from transient flare-ups.
- When no historical observations exist (new facility, or first 30 days of
  operation), a fallback FacilityBaseline is returned with is_fallback=True and
  a high uncertainty standard deviation. Callers must propagate this uncertainty
  into downstream emission estimates.
- Baselines should be recomputed weekly; the computed_at timestamp and
  methodology_version on each record let the pipeline detect stale values.

Methodology reference: methodology/v1.0.pdf §3.3 — "Background FRP Subtraction
and Baseline Windows".
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.provenance.store import ProvenanceStore

log = logging.getLogger(__name__)

BASELINE_WINDOW_DAYS: Final[int] = 30

# Fallback values when no historical data exists.
# baseline_frp_mw=0 paired with a high std expresses that we have no estimate
# of background flaring; the wide std propagates high uncertainty forward into
# the persistence and emissions steps.
FALLBACK_BASELINE_FRP_MW: Final[float] = 0.0

# FALLBACK_BASELINE_STD_MW — judgment call, not from a single paper.
# Rationale: published FIRMS-based surveys of oil/gas facility routine flaring
# (Elvidge et al. 2016 "Methods for Global Survey of Natural Gas Flaring from
# VIIRS Data"; Freeborn et al. 2014 "Relationships between energy release, fuel
# mass, fuel moisture and laboratory fire radiative power") document routine
# refinery FRP in the range 5–100 MW for large Middle East facilities.  A std
# of 50 MW means the implied 2σ interval (0–100 MW) brackets that full range,
# making it a deliberately wide prior that avoids false-positive persistence
# calls on new facilities with no characterised baseline.
# This value MUST be replaced with an empirically derived per-facility std once
# ≥ 7 days of cloud-free FIRMS observations are available for the facility.
FALLBACK_BASELINE_STD_MW: Final[float] = 50.0


class FacilityBaseline(BaseModel):
    """Rolling background FRP for one registered Facility.

    Parameters
    ----------
    facility_id : UUID
        Facility this baseline belongs to.
    baseline_frp_mw : float
        75th-percentile FRP over the rolling window, in MW. Used as the
        "normal" background level to subtract from candidate FRP.
    baseline_std_mw : float
        Robust standard deviation of FRP (IQR/1.349) over the rolling window,
        in MW. High values (≥ FALLBACK_BASELINE_STD_MW) indicate high
        uncertainty.
    n_observations : int
        Number of hotspot observations used in this baseline. Zero for
        fallback baselines with no historical data.
    window_start : AwareDatetime
        Start of the baseline computation window (UTC).
    window_end : AwareDatetime
        End of the baseline computation window (UTC).
    computed_at : AwareDatetime
        When this baseline was computed (UTC). Use to detect stale entries.
    is_fallback : bool
        True when there were no historical observations and the returned
        statistics are the module-level fallback constants, not empirical values.
    provenance_id : UUID
        ID of the ProvenanceRecord that produced this baseline.
    """

    model_config = ConfigDict(frozen=True)

    facility_id: UUID
    baseline_frp_mw: float = Field(ge=0.0)
    baseline_std_mw: float = Field(ge=0.0)
    n_observations: int = Field(ge=0)
    window_start: AwareDatetime
    window_end: AwareDatetime
    computed_at: AwareDatetime
    is_fallback: bool = False
    provenance_id: UUID


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_in_active_window(
    ts: datetime,
    windows: list[tuple[datetime, datetime]],
) -> bool:
    """Return True if *ts* falls within any of the supplied active-event windows.

    Parameters
    ----------
    ts : datetime
        Observation timestamp (must be timezone-aware).
    windows : list[tuple[datetime, datetime]]
        Active-event windows as (start, end) pairs (both inclusive, UTC).

    Returns
    -------
    bool
        True if *ts* is contained in at least one window.
    """
    return any(start <= ts <= end for start, end in windows)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_baseline(
    facility_id: UUID,
    historical_frp: list[tuple[datetime, float]],
    *,
    window_days: int = BASELINE_WINDOW_DAYS,
    active_event_windows: list[tuple[datetime, datetime]] | None = None,
    store: ProvenanceStore,
    reference_time: datetime | None = None,
) -> FacilityBaseline:
    """Compute the rolling background FRP baseline for a single Facility.

    Takes the 75th percentile and IQR-based robust standard deviation of all
    non-active-window FRP observations in the *window_days* days prior to
    *reference_time*.

    If no qualifying observations exist, returns a fallback FacilityBaseline
    with is_fallback=True and a high std (FALLBACK_BASELINE_STD_MW) to
    propagate the uncertainty conservatively into downstream steps.

    Parameters
    ----------
    facility_id : UUID
        Facility identifier. Used only to label the output; no DB call is made.
    historical_frp : list[tuple[datetime, float]]
        Historical observations as (timestamp, frp_mw) pairs. May be empty.
        Timestamps must be timezone-aware.
    window_days : int
        Rolling window length in days. Default 30.
    active_event_windows : list[tuple[datetime, datetime]] or None
        Periods of known fire activity to exclude from the baseline. Defaults
        to no exclusions.
    store : ProvenanceStore
        Receives one ProvenanceRecord recording the computation.
    reference_time : datetime or None
        Anchor for the rolling window end. Defaults to UTC now.

    Returns
    -------
    FacilityBaseline
        Populated with empirical statistics or fallback values.
    """
    now = reference_time or datetime.now(tz=UTC)
    window_start = now - timedelta(days=window_days)
    exclusions = active_event_windows or []

    # Filter to the rolling window and exclude active-event observations.
    qualifying: list[float] = [
        frp
        for ts, frp in historical_frp
        if window_start <= ts <= now and not _is_in_active_window(ts, exclusions)
    ]

    is_fallback = len(qualifying) == 0

    if is_fallback:
        baseline_frp = FALLBACK_BASELINE_FRP_MW
        baseline_std = FALLBACK_BASELINE_STD_MW
        log.warning(
            "compute_baseline: facility %s has no historical FRP in the %d-day window "
            "— returning fallback (frp=%.1f MW, std=%.1f MW)",
            facility_id,
            window_days,
            baseline_frp,
            baseline_std,
        )
    else:
        import statistics

        sorted_vals = sorted(qualifying)
        n = len(sorted_vals)
        baseline_frp = float(statistics.quantiles(sorted_vals, n=4)[2]) if n >= 2 else sorted_vals[0]
        if n >= 4:
            q1 = float(statistics.quantiles(sorted_vals, n=4)[0])
            q3 = baseline_frp
            baseline_std = (q3 - q1) / 1.349
        elif n > 1:
            baseline_std = statistics.pstdev(qualifying)
        else:
            baseline_std = 0.0
        log.debug(
            "compute_baseline: facility %s — %d obs, p75=%.2f MW, robust_std=%.2f MW",
            facility_id,
            len(qualifying),
            baseline_frp,
            baseline_std,
        )

    rec = ProvenanceRecord(
        produced_by="wced.detect.baseline",
        inputs=[],  # derived from raw observations, not a prior ProvenanceRecord
        method="rolling_p75_baseline_v1.0.1",
        parameters={
            "window_days": window_days,
            "n_observations_raw": len(historical_frp),
            "n_observations_used": len(qualifying),
            "n_excluded_active_windows": len(exclusions),
            "is_fallback": is_fallback,
            "reference_time": now.isoformat(),
        },
        produced_at=now,
        confidence_label=ConfidenceLabel.SUSPECTED if is_fallback else ConfidenceLabel.REPORTED,
        notes="No historical observations; fallback constants used." if is_fallback else None,
    )
    store.record_provenance(rec)

    return FacilityBaseline(
        facility_id=facility_id,
        baseline_frp_mw=baseline_frp,
        baseline_std_mw=baseline_std,
        n_observations=len(qualifying),
        window_start=window_start,
        window_end=now,
        computed_at=now,
        is_fallback=is_fallback,
        provenance_id=rec.id,
    )


def subtract_baseline(
    candidate_frp_mw: float,
    baseline: FacilityBaseline,
) -> tuple[float, float]:
    """Subtract facility background FRP from a candidate's FRP.

    Returns the excess FRP (zero-floored) and the propagated uncertainty.
    The excess is what is attributable to an anomalous event; routine flaring
    at the baseline level is NOT attributed.

    Parameters
    ----------
    candidate_frp_mw : float
        Peak or mean FRP from the CandidateFireEvent, in MW.
    baseline : FacilityBaseline
        Pre-computed facility background baseline.

    Returns
    -------
    tuple[float, float]
        (excess_frp_mw, uncertainty_mw).
        excess_frp_mw ≥ 0. Values below 0 before flooring indicate the
        candidate's FRP is within normal background variation.
        uncertainty_mw equals baseline.baseline_std_mw — the baseline
        standard deviation propagated as a symmetric uncertainty on the
        excess estimate.
    """
    excess = max(0.0, candidate_frp_mw - baseline.baseline_frp_mw)
    uncertainty = baseline.baseline_std_mw
    return excess, uncertainty
