"""CO2 estimate aggregation across fire events.

Aggregation sums independent :class:`Distribution` objects using
sample-wise addition — the standard IGGAW (Independent Gaussian
Approximation of Weighted sums) methodology assumption.

**Independence assumption (methodology §3.5, aggregation note):**
Sample-wise addition is statistically correct when the events being
summed are independent: i.e., distinct facilities, distinct satellite
passes, and distinct FRP integrals. For our use case this is
approximately true because (a) each FireEvent is attributed to exactly
one facility and (b) the MC draws for each event are seeded separately
at the quantification step. The resulting joint distribution is the
product of marginals, so element-wise summation produces a sample from
the true sum distribution. The approximation breaks down only when two
events share a facility (double-counting risk), which is prevented
upstream by ``wced.detect.persistence`` grouping overpasses into a
single FireEvent per facility per incident.

**Resampling:** Each distribution's sample array is resampled to
``_AGGREGATE_N_SAMPLES`` with replacement (bootstrap) before summing.
This aligns arrays of different lengths (e.g. FRP-only events at 10 000
samples vs. envelope events at 20 000) without biasing the marginal.

**Exclusion policy (methodology §3.5):**
Only events with ``reconciled_ok=True`` *and* a non-``None``
``final_distribution`` are included. Events with ``needs_review=True``
are excluded and logged at WARNING level with their event IDs. This
means dashboarded aggregates always come from editorially-cleared
estimates only.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date
from typing import Iterable
from uuid import UUID

import numpy as np

from wced.models.event import FireEvent
from wced.models.facility import Facility, FacilityType
from wced.quantify.distribution import Distribution
from wced.quantify.reconcile import ReconciliationResult

__all__ = [
    "EventEstimate",
    "FacilityID",
    "CountryCode",
    "aggregate_daily",
    "aggregate_cumulative",
    "aggregate_by_facility",
    "aggregate_by_country",
    "aggregate_by_facility_type",
]

log = logging.getLogger(__name__)

# (FireEvent, ReconciliationResult) — the primary input type for all aggregators.
EventEstimate = tuple[FireEvent, ReconciliationResult]

# Type aliases for return-dict keys.
FacilityID = UUID
CountryCode = str  # ISO 3166-1 alpha-3

# Fixed sample count for all aggregated distributions. Using a constant
# ensures every aggregate is directly comparable across windows and methods.
_AGGREGATE_N_SAMPLES: int = 10_000

# Stable provenance namespace for aggregation steps (distinct from quantify
# namespaces so audit-trail walks can identify the step type from the UUID).
_AGG_PROV_NS = uuid.UUID("c2e3f4a5-0000-5000-8000-000000000006")

# Methodology version string shared by all V1 quantification outputs.
_METHODOLOGY_VERSION = "1.0.5"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_provenance_id(prov_ids: Iterable[UUID], agg_label: str) -> UUID:
    """Derive a deterministic provenance UUID for an aggregation step.

    Sorts input IDs so the result is order-independent: the same set of
    events always produces the same aggregate provenance_id regardless of
    the order they were passed in.
    """
    sorted_ids = "|".join(sorted(str(p) for p in prov_ids))
    return uuid.uuid5(_AGG_PROV_NS, f"{agg_label}|inputs=[{sorted_ids}]")


def _filter_eligible(
    events: list[EventEstimate],
) -> list[tuple[FireEvent, Distribution]]:
    """Return (event, final_distribution) pairs that pass the inclusion gate.

    Excludes events where ``needs_review=True``, ``reconciled_ok=False``,
    or ``final_distribution is None``. All excluded events are logged at
    WARNING so the editorial board can audit which events were dropped.
    """
    eligible: list[tuple[FireEvent, Distribution]] = []
    for event, result in events:
        excluded = (
            result.needs_review
            or not result.reconciled_ok
            or result.final_distribution is None
        )
        if excluded:
            log.warning(
                "Event excluded from aggregate",
                extra={
                    "event_id": str(event.id),
                    "facility_id": str(event.facility_id),
                    "needs_review": result.needs_review,
                    "reconciled_ok": result.reconciled_ok,
                    "has_final": result.final_distribution is not None,
                    "review_reason": result.review_reason,
                },
            )
        else:
            # final_distribution is non-None here (guarded above).
            eligible.append((event, result.final_distribution))  # type: ignore[arg-type]
    return eligible


def _sum_distributions(
    dists: list[Distribution],
    provenance_id: UUID,
    rng: np.random.Generator,
) -> Distribution:
    """Sum a list of Distributions sample-wise into one aggregate Distribution.

    Empty list returns ``Distribution.constant(0.0, "tCO2e", ...)``.
    Raises if distributions have inconsistent methodology versions or units.

    Each distribution is resampled with replacement to ``_AGGREGATE_N_SAMPLES``
    before summation to align arrays of different lengths. See module docstring
    for the independence assumption that makes element-wise summation valid.
    """
    if not dists:
        return Distribution.constant(
            0.0, "tCO2e", _METHODOLOGY_VERSION, provenance_id
        )

    versions = {d.methodology_version for d in dists}
    if len(versions) > 1:
        raise ValueError(
            f"Cannot aggregate Distributions from different methodology versions: "
            f"{versions!r}. Mixing versions corrupts the audit trail. "
            "Recompute all estimates under a single methodology version first."
        )

    units_set = {d.units for d in dists}
    if len(units_set) > 1:
        raise ValueError(
            f"Cannot aggregate Distributions with different units: {units_set!r}. "
            "All emission estimates in WCED v1 must be in tCO2e."
        )

    version = next(iter(versions))
    unit = next(iter(units_set))

    # Resample + sum. For independent events E[X1+...+Xn] = ΣE[Xi] and the
    # sample-wise sum is an unbiased draw from the sum distribution.
    total = np.zeros(_AGGREGATE_N_SAMPLES, dtype=float)
    for d in dists:
        s = d._require_samples()
        # Bootstrap resample to _AGGREGATE_N_SAMPLES preserves marginal.
        idx = rng.integers(0, len(s), size=_AGGREGATE_N_SAMPLES)
        total += s[idx]

    return Distribution.from_samples(total, unit, version, provenance_id)


def _make_rng(provenance_id: UUID) -> np.random.Generator:
    """Seed an RNG from a provenance_id for reproducible resampling."""
    return np.random.default_rng(provenance_id.int % (2**32))


# ---------------------------------------------------------------------------
# Public aggregation functions
# ---------------------------------------------------------------------------


def aggregate_daily(
    events: list[EventEstimate],
    day: date,
) -> Distribution:
    """Sum eligible event emissions detected on *day* (UTC).

    Parameters
    ----------
    events : list[EventEstimate]
        All (FireEvent, ReconciliationResult) pairs in the dataset.
        Events outside *day* are ignored; events failing the eligibility
        gate (``needs_review=True``, etc.) are excluded and logged.
    day : date
        The calendar date in UTC to aggregate over. Events are matched on
        ``event.detected_at.astimezone(UTC).date() == day``.

    Returns
    -------
    Distribution
        Sum of eligible daily emissions in tCO2e. Returns
        ``Distribution.constant(0.0, ...)`` when no eligible events
        fall on *day*.
    """
    eligible = _filter_eligible(events)
    day_pairs = [
        (ev, dist)
        for ev, dist in eligible
        if ev.detected_at.astimezone(UTC).date() == day
    ]
    agg_label = f"daily:{day.isoformat()}"
    prov_id = _make_provenance_id(
        (dist.provenance_id for _, dist in day_pairs), agg_label
    )
    rng = _make_rng(prov_id)
    return _sum_distributions([dist for _, dist in day_pairs], prov_id, rng)


def aggregate_cumulative(
    events: list[EventEstimate],
    until_date: date,
) -> Distribution:
    """Sum eligible event emissions detected on or before *until_date* (UTC).

    Parameters
    ----------
    events : list[EventEstimate]
        All (FireEvent, ReconciliationResult) pairs. Events after
        *until_date* are ignored.
    until_date : date
        Inclusive upper bound (UTC date). Events detected on this date
        are included.

    Returns
    -------
    Distribution
        Cumulative sum in tCO2e from the earliest event through *until_date*.
        Returns ``Distribution.constant(0.0, ...)`` for empty input.
    """
    eligible = _filter_eligible(events)
    window_pairs = [
        (ev, dist)
        for ev, dist in eligible
        if ev.detected_at.astimezone(UTC).date() <= until_date
    ]
    agg_label = f"cumulative:until={until_date.isoformat()}"
    prov_id = _make_provenance_id(
        (dist.provenance_id for _, dist in window_pairs), agg_label
    )
    rng = _make_rng(prov_id)
    return _sum_distributions([dist for _, dist in window_pairs], prov_id, rng)


def aggregate_by_facility(
    events: list[EventEstimate],
) -> dict[FacilityID, Distribution]:
    """Sum eligible emissions grouped by facility ID.

    Parameters
    ----------
    events : list[EventEstimate]
        All (FireEvent, ReconciliationResult) pairs. Each event's
        ``facility_id`` is used as the grouping key.

    Returns
    -------
    dict[UUID, Distribution]
        One Distribution per facility that has at least one eligible
        event. Empty dict when no events pass the eligibility gate.
    """
    eligible = _filter_eligible(events)

    groups: dict[UUID, list[Distribution]] = defaultdict(list)
    for ev, dist in eligible:
        groups[ev.facility_id].append(dist)

    result: dict[FacilityID, Distribution] = {}
    for facility_id, dists in groups.items():
        agg_label = f"by_facility:{facility_id}"
        prov_id = _make_provenance_id(
            (d.provenance_id for d in dists), agg_label
        )
        rng = _make_rng(prov_id)
        result[facility_id] = _sum_distributions(dists, prov_id, rng)

    return result


def aggregate_by_country(
    events: list[EventEstimate],
    facility_map: Mapping[UUID, Facility],
) -> dict[CountryCode, Distribution]:
    """Sum eligible emissions grouped by ISO 3166-1 alpha-3 country code.

    Parameters
    ----------
    events : list[EventEstimate]
        All (FireEvent, ReconciliationResult) pairs.
    facility_map : Mapping[UUID, Facility]
        Lookup from ``facility_id`` to :class:`Facility`. Events whose
        ``facility_id`` is absent from *facility_map* are excluded and
        logged at WARNING — they cannot be attributed to a country.

    Returns
    -------
    dict[CountryCode, Distribution]
        One Distribution per country that has at least one eligible
        event with a known facility. Empty dict when none qualify.
    """
    eligible = _filter_eligible(events)

    groups: dict[str, list[Distribution]] = defaultdict(list)
    for ev, dist in eligible:
        facility = facility_map.get(ev.facility_id)
        if facility is None:
            log.warning(
                "Event excluded from country aggregate: facility not in facility_map",
                extra={"event_id": str(ev.id), "facility_id": str(ev.facility_id)},
            )
            continue
        groups[facility.country].append(dist)

    result: dict[CountryCode, Distribution] = {}
    for country, dists in groups.items():
        agg_label = f"by_country:{country}"
        prov_id = _make_provenance_id(
            (d.provenance_id for d in dists), agg_label
        )
        rng = _make_rng(prov_id)
        result[country] = _sum_distributions(dists, prov_id, rng)

    return result


def aggregate_by_facility_type(
    events: list[EventEstimate],
    facility_map: Mapping[UUID, Facility],
) -> dict[FacilityType, Distribution]:
    """Sum eligible emissions grouped by facility type.

    Parameters
    ----------
    events : list[EventEstimate]
        All (FireEvent, ReconciliationResult) pairs.
    facility_map : Mapping[UUID, Facility]
        Lookup from ``facility_id`` to :class:`Facility`. Events absent
        from *facility_map* are excluded and logged at WARNING.

    Returns
    -------
    dict[FacilityType, Distribution]
        One Distribution per :class:`FacilityType` that has at least
        one eligible event with a known facility. Empty dict when none
        qualify.
    """
    eligible = _filter_eligible(events)

    groups: dict[FacilityType, list[Distribution]] = defaultdict(list)
    for ev, dist in eligible:
        facility = facility_map.get(ev.facility_id)
        if facility is None:
            log.warning(
                "Event excluded from facility-type aggregate: facility not in facility_map",
                extra={"event_id": str(ev.id), "facility_id": str(ev.facility_id)},
            )
            continue
        groups[facility.facility_type].append(dist)

    result: dict[FacilityType, Distribution] = {}
    for ftype, dists in groups.items():
        agg_label = f"by_facility_type:{ftype.value}"
        prov_id = _make_provenance_id(
            (d.provenance_id for d in dists), agg_label
        )
        rng = _make_rng(prov_id)
        result[ftype] = _sum_distributions(dists, prov_id, rng)

    return result
