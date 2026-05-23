"""Facility registry endpoints."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from wced.api.dependencies import DbSession
from wced.api.schemas.responses import (
    FacilityDetailResponse,
    FacilityListResponse,
    FacilitySummary,
    PaginationMeta,
)
from wced.db import models

router = APIRouter(prefix="/v1/facilities", tags=["facilities"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("", response_model=FacilityListResponse, responses={422: {"description": "Validation error"}})
def list_facilities(
    db: DbSession,
    country: str | None = Query(None, description="Filter by ISO-3 country code"),
    facility_type: str | None = Query(None, description="Filter by facility type"),
    page: int = Query(1, ge=1, le=10000),
    per_page: int = Query(50, ge=1, le=200),
) -> FacilityListResponse:
    """Paginated facility registry listing."""
    fa = models.facilities
    centroid = func.ST_Centroid(fa.c.geometry)

    filters = []
    if country:
        filters.append(fa.c.country == country.upper())
    if facility_type:
        filters.append(fa.c.facility_type == facility_type.upper())

    count_q = select(func.count()).select_from(fa)
    for f in filters:
        count_q = count_q.where(f)
    total = db.execute(count_q).scalar_one()

    stmt = select(
        fa.c.id, fa.c.name, fa.c.facility_type, fa.c.country,
        fa.c.capacity_barrels, fa.c.operator,
        func.ST_Y(centroid).label("latitude"),
        func.ST_X(centroid).label("longitude"),
    )
    for f in filters:
        stmt = stmt.where(f)
    stmt = stmt.order_by(fa.c.name).offset((page - 1) * per_page).limit(per_page)
    rows = db.execute(stmt).all()

    data = [
        FacilitySummary(
            id=r.id, name=r.name, facility_type=r.facility_type,
            country=r.country, capacity_barrels=r.capacity_barrels,
            operator=r.operator, latitude=r.latitude, longitude=r.longitude,
        )
        for r in rows
    ]

    return FacilityListResponse(
        generated_at=_now(),
        data=data,
        pagination=PaginationMeta(
            total=total, page=page, per_page=per_page,
            pages=max(1, math.ceil(total / per_page)),
        ),
    )


@router.get("/{facility_id}", response_model=FacilityDetailResponse, responses={404: {"description": "Facility not found"}, 422: {"description": "Validation error"}})
def get_facility(facility_id: UUID, db: DbSession) -> FacilityDetailResponse:
    """Single facility with event count and cumulative emissions."""
    fa = models.facilities
    row = db.execute(
        select(
            fa.c.id, fa.c.name, fa.c.facility_type, fa.c.country,
            fa.c.capacity_barrels, fa.c.operator,
            func.ST_AsText(fa.c.geometry).label("geometry_wkt"),
        ).where(fa.c.id == facility_id)
    ).first()
    if row is None:
        raise HTTPException(404, detail=f"Facility {facility_id} not found")

    fe = models.fire_events
    event_count: int = db.execute(
        select(func.count()).select_from(fe).where(fe.c.facility_id == facility_id)
    ).scalar_one()

    ee = models.emission_estimates
    rn = func.row_number().over(
        partition_by=ee.c.event_id,
        order_by=[ee.c.methodology_version.desc(), ee.c.created_at.desc()],
    ).label("rn")
    latest = (
        select(ee.c.p50, rn)
        .join(fe, ee.c.event_id == fe.c.id)
        .where(fe.c.facility_id == facility_id, fe.c.status == "PUBLISHED")
        .cte("latest_est")
    )
    total_p50 = db.execute(
        select(func.coalesce(func.sum(latest.c.p50), 0.0))
        .where(latest.c.rn == 1)
    ).scalar_one()

    return FacilityDetailResponse(
        generated_at=_now(),
        data=FacilitySummary(
            id=row.id, name=row.name, facility_type=row.facility_type,
            country=row.country, capacity_barrels=row.capacity_barrels,
            operator=row.operator,
        ),
        geometry_wkt=row.geometry_wkt or "",
        event_count=event_count,
        total_p50_tco2e=float(total_p50),
    )
