"""Weekly TROPOMI validation pipeline.

Selects the top-10 events by p50 emissions from the past week, runs TROPOMI
plume detection and HYSPLIT back-calculation for each, compares top-down
estimates against bottom-up FRP/inventory numbers, and flags events whose
discrepancy ratio exceeds 2× for methodology review.

Flow:
  1. select_events_for_validation — top 10 by p50 from past week
  2. For each event:
     a. Fetch TROPOMI NO2/CO data
     b. Apply bias correction with ProvenanceRecord
     c. Run plume back-calculation
     d. Compare to bottom-up estimates
     e. Compute discrepancy ratio
     f. Flag for review if ratio > 2× for ≥3 events
  3. Write ValidationReport record
  4. Update ValidationDashboard view

Methodology reference: methodology/v1.0.pdf §3.6.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

from wced.models.event import EventStatus, FireEvent
from wced.models.facility import Facility
from wced.models.provenance import ConfidenceLabel, ProvenanceRecord
from wced.pipeline.quantification import EmissionEstimate
from wced.validate.tropomi import (
    BackCalculation,
    DiscrepancyResult,
    PlumeDetection,
    back_calculate_emissions,
    compute_discrepancy,
    detect_no2_plume_at,
)

log = logging.getLogger(__name__)

_MODULE = "wced.pipeline.validation_weekly"
_PROV_NS = uuid.UUID("e5f6a7b8-0000-5000-8000-000000000020")

_TOP_N_EVENTS: int = 10
_DISCREPANCY_FLAG_THRESHOLD: int = 3
_METHODOLOGY_REVIEW_RATIO: float = 2.0


@dataclass(frozen=True)
class EventValidation:
    """Validation result for a single event.

    Parameters
    ----------
    event : FireEvent
        The event that was validated.
    facility : Facility
        The facility the event is attributed to.
    bottom_up_p50 : float
        Median of the bottom-up reconciled estimate (tCO2e).
    plume : PlumeDetection or None
        TROPOMI plume detection result. None if retrieval failed.
    back_calc : BackCalculation or None
        HYSPLIT back-calculation. None if no plume detected or retrieval failed.
    discrepancy : DiscrepancyResult or None
        Discrepancy comparison. None if back-calculation unavailable.
    error : str or None
        Error message if validation failed for this event.
    """

    event: FireEvent
    facility: Facility
    bottom_up_p50: float
    plume: PlumeDetection | None = None
    back_calc: BackCalculation | None = None
    discrepancy: DiscrepancyResult | None = None
    error: str | None = None


class ValidationReport(BaseModel):
    """Aggregate report from a weekly validation run.

    Parameters
    ----------
    id : UUID
        Stable identifier for this report.
    run_date : date
        Date this validation was executed.
    week_start : date
        Start of the validation window (inclusive).
    week_end : date
        End of the validation window (inclusive).
    events_selected : int
        Number of events selected for validation.
    events_validated : int
        Number of events that completed validation successfully.
    events_with_plume : int
        Events where a TROPOMI plume was detected.
    events_flagged : int
        Events where discrepancy ratio > 2×.
    methodology_review_triggered : bool
        True iff ≥3 events flagged (triggers methodology review).
    flagged_event_ids : list[UUID]
        IDs of events flagged for review.
    provenance_record : ProvenanceRecord
        Provenance for this validation run.
    computed_at : AwareDatetime
        When this report was produced.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: UUID
    run_date: date
    week_start: date
    week_end: date
    events_selected: int
    events_validated: int
    events_with_plume: int
    events_flagged: int
    methodology_review_triggered: bool
    flagged_event_ids: list[UUID]
    provenance_record: ProvenanceRecord
    computed_at: AwareDatetime


def select_events_for_validation(
    estimates: list[EmissionEstimate],
    events: list[FireEvent],
    week_end: date | None = None,
    top_n: int = _TOP_N_EVENTS,
) -> list[tuple[FireEvent, float]]:
    """Select the top N events by p50 emissions from the past week.

    Parameters
    ----------
    estimates : list[EmissionEstimate]
        All available emission estimates.
    events : list[FireEvent]
        All fire events (used for date filtering).
    week_end : date or None
        End of the validation window. Defaults to today (UTC).
    top_n : int
        Maximum number of events to select.

    Returns
    -------
    list[tuple[FireEvent, float]]
        Selected (event, p50_tCO2e) pairs, sorted descending by p50.
    """
    if week_end is None:
        week_end = datetime.now(UTC).date()
    week_start = week_end - timedelta(days=7)

    event_map: dict[UUID, FireEvent] = {e.id: e for e in events}

    candidates: list[tuple[FireEvent, float]] = []
    for est in estimates:
        ev = event_map.get(est.event_id)
        if ev is None:
            continue
        if ev.status != EventStatus.PUBLISHED:
            continue

        event_date = ev.detected_at.date()
        if not (week_start <= event_date <= week_end):
            continue

        recon = est.reconciliation
        if recon.final_distribution is None:
            continue
        if recon.needs_review:
            continue

        candidates.append((ev, recon.final_distribution.p50))

    candidates.sort(key=lambda x: x[1], reverse=True)
    selected = candidates[:top_n]

    log.info(
        "select_events_for_validation",
        extra={
            "week_start": str(week_start),
            "week_end": str(week_end),
            "candidates": len(candidates),
            "selected": len(selected),
        },
    )

    return selected


def validate_event(
    event: FireEvent,
    facility: Facility,
    bottom_up_p50: float,
    *,
    wind_speed_m_s: float = 5.0,
    wind_direction_deg: float = 270.0,
    product: str = "NO2",
) -> EventValidation:
    """Run TROPOMI validation for a single event.

    Parameters
    ----------
    event : FireEvent
        The event to validate.
    facility : Facility
        The facility (used for coordinates).
    bottom_up_p50 : float
        Median of the bottom-up estimate (tCO2e).
    wind_speed_m_s : float
        Representative wind speed (m/s). Default 5.0.
    wind_direction_deg : float
        Wind direction in degrees. Default 270.0 (westerly).
    product : str
        TROPOMI product to use ("NO2" or "CO").

    Returns
    -------
    EventValidation
        Complete validation result for this event.
    """
    from shapely import wkt as shapely_wkt

    geom = shapely_wkt.loads(facility.geometry_wkt)
    if geom.geom_type == "Point":
        lat, lon = geom.y, geom.x
    else:
        centroid = geom.centroid
        lat, lon = centroid.y, centroid.x

    try:
        plume = detect_no2_plume_at(
            event, lat, lon, product=product,
        )
    except Exception as exc:
        log.warning(
            "validate_event: TROPOMI retrieval failed",
            extra={"event_id": str(event.id), "error": str(exc)},
        )
        return EventValidation(
            event=event,
            facility=facility,
            bottom_up_p50=bottom_up_p50,
            error=f"TROPOMI retrieval failed: {exc}",
        )

    if not plume.plume_detected:
        log.info(
            "validate_event: no plume detected",
            extra={"event_id": str(event.id)},
        )
        return EventValidation(
            event=event,
            facility=facility,
            bottom_up_p50=bottom_up_p50,
            plume=plume,
        )

    try:
        back_calc = back_calculate_emissions(
            plume,
            wind_speed_m_s=wind_speed_m_s,
            wind_direction_deg=wind_direction_deg,
        )
    except Exception as exc:
        log.warning(
            "validate_event: back-calculation failed",
            extra={"event_id": str(event.id), "error": str(exc)},
        )
        return EventValidation(
            event=event,
            facility=facility,
            bottom_up_p50=bottom_up_p50,
            plume=plume,
            error=f"Back-calculation failed: {exc}",
        )

    discrepancy = compute_discrepancy(event, back_calc, bottom_up_p50)

    return EventValidation(
        event=event,
        facility=facility,
        bottom_up_p50=bottom_up_p50,
        plume=plume,
        back_calc=back_calc,
        discrepancy=discrepancy,
    )


def weekly_validation(
    estimates: list[EmissionEstimate],
    events: list[FireEvent],
    facility_map: Mapping[UUID, Facility],
    *,
    week_end: date | None = None,
    wind_speed_m_s: float = 5.0,
    wind_direction_deg: float = 270.0,
    product: str = "NO2",
) -> ValidationReport:
    """Run the weekly TROPOMI validation pipeline.

    Parameters
    ----------
    estimates : list[EmissionEstimate]
        All emission estimates (filtered to past week internally).
    events : list[FireEvent]
        All fire events.
    facility_map : Mapping[UUID, Facility]
        Facility lookup by ID.
    week_end : date or None
        End of validation window. Defaults to today (UTC).
    wind_speed_m_s : float
        Representative wind speed for HYSPLIT (m/s).
    wind_direction_deg : float
        Wind direction for HYSPLIT (degrees).
    product : str
        TROPOMI product ("NO2" or "CO").

    Returns
    -------
    ValidationReport
        Complete validation report with methodology review flag.
    """
    if week_end is None:
        week_end = datetime.now(UTC).date()
    week_start = week_end - timedelta(days=7)

    selected = select_events_for_validation(
        estimates, events, week_end=week_end,
    )

    validations: list[EventValidation] = []
    for event, p50 in selected:
        facility = facility_map.get(event.facility_id)
        if facility is None:
            log.warning(
                "weekly_validation: facility not found",
                extra={
                    "event_id": str(event.id),
                    "facility_id": str(event.facility_id),
                },
            )
            continue

        result = validate_event(
            event, facility, p50,
            wind_speed_m_s=wind_speed_m_s,
            wind_direction_deg=wind_direction_deg,
            product=product,
        )
        validations.append(result)

    events_with_plume = sum(
        1 for v in validations
        if v.plume is not None and v.plume.plume_detected
    )
    flagged = [
        v for v in validations
        if v.discrepancy is not None and v.discrepancy.flagged
    ]
    flagged_ids = [v.event.id for v in flagged]
    review_triggered = len(flagged) >= _DISCREPANCY_FLAG_THRESHOLD

    if review_triggered:
        log.warning(
            "weekly_validation: METHODOLOGY REVIEW TRIGGERED",
            extra={
                "flagged_count": len(flagged),
                "threshold": _DISCREPANCY_FLAG_THRESHOLD,
                "flagged_event_ids": [str(eid) for eid in flagged_ids],
            },
        )

    run_id = uuid.uuid5(
        _PROV_NS, f"weekly_validation|{week_start}|{week_end}",
    )

    provenance_inputs = []
    for v in validations:
        if v.plume is not None:
            provenance_inputs.append(v.plume.bias_correction_record.id)
        if v.back_calc is not None:
            provenance_inputs.append(v.back_calc.provenance_record.id)

    prov_record = ProvenanceRecord(
        id=run_id,
        produced_by=_MODULE,
        inputs=provenance_inputs,
        method="weekly_tropomi_validation_v1.0",
        parameters={
            "week_start": str(week_start),
            "week_end": str(week_end),
            "top_n": _TOP_N_EVENTS,
            "product": product,
            "wind_speed_m_s": wind_speed_m_s,
            "wind_direction_deg": wind_direction_deg,
            "discrepancy_flag_threshold": _DISCREPANCY_FLAG_THRESHOLD,
        },
        produced_at=datetime.now(tz=UTC),
        confidence_label=ConfidenceLabel.SUSPECTED,
        notes=(
            "Weekly TROPOMI top-down validation — methodology §3.6. "
            "Research-grade with factor-of-2 uncertainty."
        ),
    )

    events_validated = sum(1 for v in validations if v.error is None)

    report = ValidationReport(
        id=run_id,
        run_date=datetime.now(UTC).date(),
        week_start=week_start,
        week_end=week_end,
        events_selected=len(selected),
        events_validated=events_validated,
        events_with_plume=events_with_plume,
        events_flagged=len(flagged),
        methodology_review_triggered=review_triggered,
        flagged_event_ids=flagged_ids,
        provenance_record=prov_record,
        computed_at=datetime.now(UTC),
    )

    log.info(
        "weekly_validation complete",
        extra={
            "events_selected": report.events_selected,
            "events_validated": report.events_validated,
            "events_with_plume": report.events_with_plume,
            "events_flagged": report.events_flagged,
            "methodology_review_triggered": report.methodology_review_triggered,
        },
    )

    return report
