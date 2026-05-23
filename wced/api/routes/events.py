"""Event endpoints — list, detail, provenance, assessment."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from uuid import UUID

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from wced.api.dependencies import DbSession
from wced.api.schemas.responses import (
    DamageAssessmentResponse,
    EmissionEstimateOut,
    EventDetailResponse,
    EventListResponse,
    EventSummary,
    PaginationMeta,
    ProvenanceNodeOut,
    ProvenanceResponse,
)
from wced.db import models

from sqlalchemy import func, select

router = APIRouter(prefix="/v1/events", tags=["events"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_event_summary(row: dict, estimate: dict | None = None) -> EventSummary:
    est = None
    if estimate:
        est = EmissionEstimateOut(
            id=estimate["id"],
            event_id=estimate["event_id"],
            methodology_version=estimate["methodology_version"],
            method=estimate["method"],
            p5=estimate["p5"],
            p50=estimate["p50"],
            p95=estimate["p95"],
            units=estimate["units"],
            created_at=estimate["created_at"],
        )
    return EventSummary(
        id=row["id"],
        facility_id=row["facility_id"],
        detected_at=row["detected_at"],
        last_seen_at=row["last_seen_at"],
        peak_frp_mw=row["peak_frp_mw"],
        total_frp_integral_mj=row["total_frp_integral_mj"],
        detection_source=row["detection_source"],
        confidence_label=row["confidence_label"],
        status=row["status"],
        notes=row["notes"],
        estimate=est,
    )


def _latest_estimate_query(
    event_id: UUID,
    methodology_version: str | None = None,
) -> select:
    """Return a query for the latest emission estimate for a given event.

    When methodology_version is None, returns the estimate with the highest
    methodology_version (lexicographic). When specified, filters to that
    exact version.
    """
    ee = models.emission_estimates
    stmt = select(ee).where(ee.c.event_id == event_id)
    if methodology_version:
        stmt = stmt.where(ee.c.methodology_version == methodology_version)
    return stmt.order_by(
        ee.c.methodology_version.desc(), ee.c.created_at.desc()
    ).limit(1)


@router.get("", response_model=EventListResponse, responses={422: {"description": "Validation error"}})
def list_events(
    db: DbSession,
    status: str | None = Query(None, description="Filter by event status"),
    from_date: date | None = Query(None, alias="from", description="Start date (inclusive)"),
    to_date: date | None = Query(None, alias="to", description="End date (inclusive)"),
    facility_type: str | None = Query(None, description="Filter by facility type"),
    methodology_version: str | None = Query(None, description="Filter estimates to a specific methodology version"),
    page: int = Query(1, ge=1, le=10000),
    per_page: int = Query(50, ge=1, le=200),
) -> EventListResponse:
    """Paginated list of fire events with their latest emission estimate."""
    fe = models.fire_events
    fa = models.facilities
    stmt = select(fe)

    if status:
        stmt = stmt.where(fe.c.status == status.upper())
    if from_date:
        stmt = stmt.where(fe.c.detected_at >= datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc))
    if to_date:
        stmt = stmt.where(fe.c.detected_at <= datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc))
    if facility_type:
        subq = select(fa.c.id).where(fa.c.facility_type == facility_type.upper())
        stmt = stmt.where(fe.c.facility_id.in_(subq))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_stmt).scalar_one()

    stmt = stmt.order_by(fe.c.detected_at.desc()).offset((page - 1) * per_page).limit(per_page)
    rows = db.execute(stmt).all()

    summaries: list[EventSummary] = []
    for r in rows:
        row_dict = r._asdict()
        est_row = db.execute(
            _latest_estimate_query(row_dict["id"], methodology_version)
        ).first()
        est_dict = est_row._asdict() if est_row else None
        summaries.append(_row_to_event_summary(row_dict, est_dict))

    return EventListResponse(
        generated_at=_now(),
        data=summaries,
        pagination=PaginationMeta(
            total=total,
            page=page,
            per_page=per_page,
            pages=max(1, math.ceil(total / per_page)),
        ),
    )


@router.get("/{event_id}", response_model=EventDetailResponse, responses={404: {"description": "Event not found"}, 422: {"description": "Validation error"}})
def get_event(
    event_id: UUID,
    db: DbSession,
    methodology_version: str | None = Query(None, description="Filter estimates to a specific methodology version"),
) -> EventDetailResponse:
    """Single event with full estimate history."""
    row = db.execute(
        select(models.fire_events).where(models.fire_events.c.id == event_id)
    ).first()
    if row is None:
        raise HTTPException(404, detail=f"Event {event_id} not found")
    row_dict = row._asdict()

    ee = models.emission_estimates
    est_stmt = (
        select(ee)
        .where(ee.c.event_id == event_id)
        .order_by(ee.c.methodology_version.desc(), ee.c.created_at.desc())
    )
    if methodology_version:
        est_stmt = est_stmt.where(ee.c.methodology_version == methodology_version)

    est_rows = db.execute(est_stmt).all()
    estimates = [
        EmissionEstimateOut(
            id=e["id"], event_id=e["event_id"],
            methodology_version=e["methodology_version"],
            method=e["method"], p5=e["p5"], p50=e["p50"], p95=e["p95"],
            units=e["units"], created_at=e["created_at"],
        )
        for e in (er._asdict() for er in est_rows)
    ]

    latest = estimates[0] if estimates else None
    return EventDetailResponse(
        generated_at=_now(),
        data=_row_to_event_summary(row_dict, latest.model_dump() if latest else None),
        estimates=estimates,
    )


@router.get("/{event_id}/provenance", response_model=ProvenanceResponse, responses={404: {"description": "Event not found"}, 422: {"description": "Validation error"}})
def get_event_provenance(event_id: UUID, db: DbSession) -> ProvenanceResponse:
    """Rendered provenance chain for an event's emission estimate."""
    row = db.execute(
        select(models.fire_events).where(models.fire_events.c.id == event_id)
    ).first()
    if row is None:
        raise HTTPException(404, detail=f"Event {event_id} not found")

    # Walk from the latest estimate's provenance_id (the quantification chain),
    # falling back to the fire_event's provenance_id (detection chain).
    ee = models.emission_estimates
    est_row = db.execute(
        select(ee.c.provenance_id)
        .where(ee.c.event_id == event_id)
        .order_by(ee.c.methodology_version.desc(), ee.c.created_at.desc())
        .limit(1)
    ).first()
    provenance_id = est_row[0] if est_row else row._asdict()["provenance_id"]

    chain_nodes: list[ProvenanceNodeOut] = []
    rendered_lines: list[str] = []
    visited: set[UUID] = set()
    queue = [provenance_id]

    while queue:
        current_id = queue.pop(0)
        if current_id in visited:
            continue
        visited.add(current_id)

        rec = db.execute(
            select(models.provenance_records).where(models.provenance_records.c.id == current_id)
        ).first()
        if rec:
            rec_dict = rec._asdict()
            chain_nodes.append(ProvenanceNodeOut(
                node_type="computation",
                id=rec_dict["id"],
                detail={
                    "produced_by": rec_dict["produced_by"],
                    "method": rec_dict["method"],
                    "confidence_label": rec_dict["confidence_label"],
                    "produced_at": rec_dict["produced_at"].isoformat() if rec_dict["produced_at"] else None,
                    "notes": rec_dict.get("notes"),
                },
            ))
            rendered_lines.append(
                f"[COMPUTATION] {rec_dict['produced_by']} / {rec_dict['method']}"
                f" [{rec_dict['confidence_label']}]"
            )
            inputs = db.execute(
                select(models.provenance_inputs)
                .where(models.provenance_inputs.c.provenance_id == current_id)
            ).all()
            for inp in inputs:
                inp_dict = inp._asdict()
                queue.append(inp_dict["input_id"])
        else:
            src = db.execute(
                select(models.sources).where(models.sources.c.id == current_id)
            ).first()
            if src:
                src_dict = src._asdict()
                chain_nodes.append(ProvenanceNodeOut(
                    node_type="source",
                    id=src_dict["id"],
                    detail={
                        "source_type": src_dict["source_type"],
                        "identifier": src_dict["identifier"],
                        "retrieved_at": src_dict["retrieved_at"].isoformat() if src_dict["retrieved_at"] else None,
                    },
                ))
                rendered_lines.append(
                    f"[{src_dict['source_type']}] {src_dict['identifier']}"
                )

    return ProvenanceResponse(
        generated_at=_now(),
        event_id=event_id,
        chain=chain_nodes,
        rendered="\n→ ".join(rendered_lines) if rendered_lines else "(no provenance chain found)",
    )


@router.get("/{event_id}/assessment", response_model=DamageAssessmentResponse, responses={404: {"description": "Event not found"}, 422: {"description": "Validation error"}})
def get_event_assessment(event_id: UUID, db: DbSession) -> DamageAssessmentResponse:
    """DamageAssessment for an event, if one exists."""
    row = db.execute(
        select(models.fire_events).where(models.fire_events.c.id == event_id)
    ).first()
    if row is None:
        raise HTTPException(404, detail=f"Event {event_id} not found")

    assessment = db.execute(
        select(models.damage_assessments)
        .where(models.damage_assessments.c.event_id == event_id)
        .order_by(models.damage_assessments.c.assessed_at.desc())
        .limit(1)
    ).first()

    return DamageAssessmentResponse(
        generated_at=_now(),
        event_id=event_id,
        data=assessment._asdict() if assessment else None,
    )


def _serve_s2_chip(event_id: UUID, phase: str, db: DbSession) -> FileResponse:
    """Serve a Sentinel-2 chip PNG for an event (before or after)."""
    row = db.execute(
        select(models.fire_events).where(models.fire_events.c.id == event_id)
    ).first()
    if row is None:
        raise HTTPException(404, detail=f"Event {event_id} not found")

    detected_at = row._asdict()["detected_at"]
    s2 = models.s2_chips
    if phase == "before":
        stmt = (
            select(s2)
            .where(s2.c.event_id == event_id, s2.c.acquisition_date < detected_at)
            .order_by(s2.c.acquisition_date.desc())
            .limit(1)
        )
    else:
        stmt = (
            select(s2)
            .where(s2.c.event_id == event_id, s2.c.acquisition_date >= detected_at)
            .order_by(s2.c.acquisition_date.asc())
            .limit(1)
        )

    chip = db.execute(stmt).first()
    if chip is None:
        raise HTTPException(404, detail=f"No {phase}-event Sentinel-2 chip for event {event_id}")

    chip_path = Path(chip._asdict()["storage_path"])
    if not chip_path.exists():
        raise HTTPException(404, detail=f"Chip file not found on disk: {chip_path}")

    return FileResponse(chip_path, media_type="image/png")


@router.get("/{event_id}/s2/before", responses={404: {"description": "No pre-event chip"}})
def get_s2_before(event_id: UUID, db: DbSession) -> FileResponse:
    """Pre-event Sentinel-2 true-color chip as PNG."""
    return _serve_s2_chip(event_id, "before", db)


@router.get("/{event_id}/s2/after", responses={404: {"description": "No post-event chip"}})
def get_s2_after(event_id: UUID, db: DbSession) -> FileResponse:
    """Post-event Sentinel-2 true-color chip as PNG."""
    return _serve_s2_chip(event_id, "after", db
    )
