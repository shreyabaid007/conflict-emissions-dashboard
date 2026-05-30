"""Quantification pipeline: compute emission estimates for published events.

Runs daily after the editorial queue is processed. For each PUBLISHED
FireEvent, computes FRP-based and (when possible) inventory-based CO2
estimates, reconciles them, and writes an ``EmissionEstimate`` record with
full provenance and methodology versioning.

Flow:
  1. Load emission factors and parameter distributions from YAML.
  2. For each published event:
     a. Compute FRP emissions (always, if ``total_frp_integral_mj`` is set).
     b. Look up a DamageAssessment for the event (human-entered during
        editorial approval). If present AND the facility has a known
        capacity, compute inventory emissions.
     c. Look up any reported third-party estimates (cross-check only).
     d. Reconcile FRP + inventory estimates per methodology §3.5.
     e. Write an EmissionEstimate record.
  3. Recompute aggregates (daily, cumulative, by facility type).
  4. Publish to API cache.

Methodology reference: methodology/v1.0.pdf §3.3–§3.5.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from wced.models.assessment import DamageAssessment
from wced.models.event import EventStatus, FireEvent
from wced.models.facility import Facility
from wced.quantify.aggregate import (
    EventEstimate,
    aggregate_by_facility,
    aggregate_by_facility_type,
    aggregate_cumulative,
    aggregate_daily,
)
from wced.quantify.distribution import Distribution
from wced.quantify.factors import (
    FactorRegistry,
    load_factors,
    load_parameter_distributions,
)
from wced.quantify.frp import compute_frp_emissions
from wced.quantify.inventory import compute_inventory_emissions
from wced.quantify.reconcile import ReconciliationResult, reconcile_estimates

__all__ = [
    "EmissionEstimate",
    "QuantificationResult",
    "quantify_event",
    "quantify_published_events",
]

log = logging.getLogger(__name__)

_METHODOLOGY_VERSION = "1.0"


class EmissionEstimate(BaseModel):
    """A complete, reconciled emission estimate for one fire event.

    Written to the database after quantification. Contains the reconciled
    headline distribution plus the individual method estimates for audit.

    Parameters
    ----------
    event_id : UUID
        The FireEvent this estimate covers.
    facility_id : UUID
        The facility the event is attributed to.
    methodology_version : str
        Semver of the methodology PDF used to produce this estimate.
    reconciliation : ReconciliationResult
        Full reconciliation record including FRP, inventory, and reported
        estimates plus the agreement ratio and review flags.
    has_damage_assessment : bool
        Whether a DamageAssessment was available for the inventory method.
    computed_at : AwareDatetime
        When this estimate was produced (UTC).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event_id: UUID
    facility_id: UUID
    methodology_version: str
    reconciliation: ReconciliationResult
    has_damage_assessment: bool
    computed_at: AwareDatetime


class QuantificationResult(BaseModel):
    """Aggregate output of ``quantify_published_events``.

    Parameters
    ----------
    estimates : list[EmissionEstimate]
        Per-event estimates for all successfully quantified events.
    skipped_event_ids : list[UUID]
        Events that could not be quantified (e.g. no FRP integral).
    event_estimates : list[EventEstimate]
        (FireEvent, ReconciliationResult) pairs for aggregation.
    methodology_version : str
        Methodology version used for this run.
    computed_at : AwareDatetime
        When the quantification run completed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    estimates: list[EmissionEstimate]
    skipped_event_ids: list[UUID]
    event_estimates: list[EventEstimate]
    methodology_version: str
    computed_at: AwareDatetime


def quantify_event(
    event: FireEvent,
    facility: Facility,
    factors: FactorRegistry,
    params: FactorRegistry,
    damage_assessment: DamageAssessment | None = None,
    reported_estimate: Distribution | None = None,
    n_samples: int = 10_000,
    rng_seed: int | None = None,
) -> EmissionEstimate:
    """Quantify emissions for a single published FireEvent.

    Parameters
    ----------
    event : FireEvent
        Must be PUBLISHED and have ``total_frp_integral_mj`` set for the
        FRP method to run.
    facility : Facility
        The facility this event is attributed to.
    factors : FactorRegistry
        Loaded ``data/emission_factors.yaml``.
    params : FactorRegistry
        Loaded ``data/parameter_distributions.yaml``.
    damage_assessment : DamageAssessment or None
        Human-entered damage assessment from editorial review. Required
        for the inventory method; when None, only FRP is computed.
    reported_estimate : Distribution or None
        Third-party estimate for cross-check (never enters headline).
    n_samples : int
        Monte Carlo draw count.
    rng_seed : int or None
        Seed for reproducibility.

    Returns
    -------
    EmissionEstimate
        Reconciled estimate with provenance.

    Raises
    ------
    ValueError
        If the event has no ``total_frp_integral_mj`` (cannot quantify).
    """
    frp_estimate: Distribution | None = None
    inventory_estimate: Distribution | None = None

    if event.total_frp_integral_mj is not None and event.total_frp_integral_mj > 0:
        frp_estimate = compute_frp_emissions(
            event, factors, n_samples=n_samples, rng_seed=rng_seed,
        )
        log.info(
            "quantify_event.frp",
            extra={
                "event_id": str(event.id),
                "p50_tCO2e": frp_estimate.p50,
            },
        )

    has_assessment = damage_assessment is not None
    if (
        has_assessment
        and facility.capacity_barrels is not None
        and facility.capacity_barrels > 0
    ):
        inventory_estimate = compute_inventory_emissions(
            event,
            facility,
            damage_assessment.fraction_destroyed_pdf,  # type: ignore[union-attr]
            factors,
            params,
            n_samples=n_samples,
            rng_seed=rng_seed,
        )
        log.info(
            "quantify_event.inventory",
            extra={
                "event_id": str(event.id),
                "p50_tCO2e": inventory_estimate.p50,
            },
        )

    reconciliation = reconcile_estimates(
        event, frp_estimate, inventory_estimate, reported_estimate,
    )

    return EmissionEstimate(
        event_id=event.id,
        facility_id=event.facility_id,
        methodology_version=_METHODOLOGY_VERSION,
        reconciliation=reconciliation,
        has_damage_assessment=has_assessment,
        computed_at=datetime.now(UTC),
    )


def quantify_published_events(
    events: list[FireEvent] | None = None,
    facility_map: Mapping[UUID, Facility] | None = None,
    assessment_map: Mapping[UUID, DamageAssessment] | None = None,
    reported_map: Mapping[UUID, Distribution] | None = None,
    methodology_version: str = _METHODOLOGY_VERSION,
    n_samples: int = 10_000,
    rng_seed: int | None = None,
) -> QuantificationResult:
    """Quantify emissions for all published events.

    This is the top-level entry point called by the daily pipeline after
    editorial review is complete.

    Parameters
    ----------
    events : list[FireEvent] or None
        Events to quantify. When None, the caller must provide events
        (the function does not query a database). Only PUBLISHED events
        are processed; others are filtered out.
    facility_map : Mapping[UUID, Facility] or None
        Lookup from ``facility_id`` → Facility. Events whose facility is
        missing are skipped with a warning.
    assessment_map : Mapping[UUID, DamageAssessment] or None
        Lookup from ``event_id`` → DamageAssessment. Events without an
        assessment skip the inventory method.
    reported_map : Mapping[UUID, Distribution] or None
        Lookup from ``event_id`` → third-party reported estimate.
    methodology_version : str
        Methodology version string. Must match the loaded factors.
    n_samples : int
        Monte Carlo draw count.
    rng_seed : int or None
        Base seed. Each event derives its own seed from this base to
        ensure reproducibility while keeping events independent.

    Returns
    -------
    QuantificationResult
        All estimates, skipped events, and (event, reconciliation) pairs
        ready for aggregation.
    """
    factors = load_factors()
    params = load_parameter_distributions()

    if events is None:
        events = []
    if facility_map is None:
        facility_map = {}
    if assessment_map is None:
        assessment_map = {}
    if reported_map is None:
        reported_map = {}

    published = [e for e in events if e.status == EventStatus.PUBLISHED]
    log.info(
        "quantify_published_events: %d published of %d total",
        len(published),
        len(events),
    )

    estimates: list[EmissionEstimate] = []
    skipped: list[UUID] = []
    event_estimates: list[EventEstimate] = []

    for i, event in enumerate(published):
        facility = facility_map.get(event.facility_id)
        if facility is None:
            log.warning(
                "quantify: skipping event — facility not found",
                extra={
                    "event_id": str(event.id),
                    "facility_id": str(event.facility_id),
                },
            )
            skipped.append(event.id)
            continue

        if (
            event.total_frp_integral_mj is None
            or event.total_frp_integral_mj <= 0
        ):
            log.warning(
                "quantify: skipping event — no FRP integral",
                extra={"event_id": str(event.id)},
            )
            skipped.append(event.id)
            continue

        event_seed = (rng_seed + i) if rng_seed is not None else None
        assessment = assessment_map.get(event.id)
        reported = reported_map.get(event.id)

        try:
            estimate = quantify_event(
                event,
                facility,
                factors,
                params,
                damage_assessment=assessment,
                reported_estimate=reported,
                n_samples=n_samples,
                rng_seed=event_seed,
            )
            estimates.append(estimate)
            event_estimates.append((event, estimate.reconciliation))
        except Exception:
            log.exception(
                "quantify: failed for event",
                extra={"event_id": str(event.id)},
            )
            skipped.append(event.id)

    log.info(
        "quantify_published_events: %d estimated, %d skipped",
        len(estimates),
        len(skipped),
    )

    return QuantificationResult(
        estimates=estimates,
        skipped_event_ids=skipped,
        event_estimates=event_estimates,
        methodology_version=methodology_version,
        computed_at=datetime.now(UTC),
    )


def recompute_aggregates(
    result: QuantificationResult,
    facility_map: Mapping[UUID, Facility],
    target_date: date | None = None,
) -> dict[str, Any]:
    """Recompute daily, cumulative, and by-facility-type aggregates.

    Parameters
    ----------
    result : QuantificationResult
        Output of ``quantify_published_events``.
    facility_map : Mapping[UUID, Facility]
        Facility lookup for type-based aggregation.
    target_date : date or None
        Date for daily aggregate. Defaults to today (UTC).

    Returns
    -------
    dict[str, Any]
        Keys: ``daily``, ``cumulative``, ``by_facility``, ``by_facility_type``.
        Values are Distribution objects or dicts thereof.
    """
    if target_date is None:
        target_date = datetime.now(UTC).date()

    event_estimates = result.event_estimates

    daily = aggregate_daily(event_estimates, target_date)
    cumulative = aggregate_cumulative(event_estimates, target_date)
    by_facility = aggregate_by_facility(event_estimates)
    by_type = aggregate_by_facility_type(event_estimates, facility_map)

    log.info(
        "recompute_aggregates",
        extra={
            "daily_p50": daily.p50,
            "cumulative_p50": cumulative.p50,
            "n_facilities": len(by_facility),
            "n_types": len(by_type),
        },
    )

    return {
        "daily": daily,
        "cumulative": cumulative,
        "by_facility": by_facility,
        "by_facility_type": by_type,
    }


def publish_to_api_cache(
    result: QuantificationResult,
    aggregates: dict[str, Any],
) -> None:
    """Publish quantification results to the API cache layer.

    Stub — implemented once the API and caching layer are in place.
    Logs the intent so pipeline runs are auditable even before the
    cache backend exists.

    Parameters
    ----------
    result : QuantificationResult
        Per-event estimates.
    aggregates : dict[str, Any]
        Output of ``recompute_aggregates``.
    """
    log.info(
        "publish_to_api_cache",
        extra={
            "n_estimates": len(result.estimates),
            "methodology_version": result.methodology_version,
            "has_aggregates": bool(aggregates),
        },
    )
