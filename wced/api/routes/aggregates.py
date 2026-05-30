"""Aggregate endpoints — by facility type and by country.

All aggregate queries use only the latest methodology-version estimate per
event (highest version string, most recent created_at as tiebreak) so that
recomputed estimates replace old ones in the headline without double-counting.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.sql import expression as sa_expr

from wced.api.dependencies import DbSession
from wced.api.schemas.responses import AggregateRow, AggregatesResponse, HeadlineResponse
from wced.db import models

# z-score for 90% CI (5th–95th percentile) used to approximate per-event
# standard deviation from stored percentiles: std ≈ (p95 - p5) / (2 * Z_90).
_Z_90 = 1.6449

router = APIRouter(prefix="/v1/aggregates", tags=["aggregates"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_estimates_cte(methodology_version: str | None = None) -> sa_expr.CTE:
    """CTE selecting the single latest emission estimate per event.

    Uses ROW_NUMBER() partitioned by event_id, ordered by
    methodology_version DESC, created_at DESC. When methodology_version
    is specified, only estimates of that version are considered.
    """
    ee = models.emission_estimates
    fe = models.fire_events

    rn = func.row_number().over(
        partition_by=ee.c.event_id,
        order_by=[ee.c.methodology_version.desc(), ee.c.created_at.desc()],
    ).label("rn")

    stmt = (
        select(ee, rn)
        .join(fe, ee.c.event_id == fe.c.id)
        .where(fe.c.status == "PUBLISHED")
    )
    if methodology_version:
        stmt = stmt.where(ee.c.methodology_version == methodology_version)

    cte = stmt.cte("latest_est")
    return cte


@router.get("/headline", response_model=HeadlineResponse)
def headline(
    db: DbSession,
    methodology_version: str | None = Query(None, description="Filter to a specific methodology version"),
) -> HeadlineResponse:
    """Top-line totals: sum of latest-version emission estimates for published events."""
    fa = models.facilities
    cte = _latest_estimates_cte(methodology_version)

    # Summing individual percentiles (sum(p5), sum(p95)) produces bounds
    # that are too wide: it assumes all events simultaneously hit their
    # extreme tails. For N independent events, CLT gives a tighter aggregate:
    #   std_i ≈ (p95_i - p5_i) / (2 * 1.645)
    #   aggregate_std = sqrt(sum(std_i²))
    #   aggregate_p5/p95 = sum(p50) ∓ 1.645 * aggregate_std
    stmt = select(
        func.coalesce(func.sum(cte.c.p50), 0.0).label("p50"),
        func.coalesce(
            func.sum(func.pow((cte.c.p95 - cte.c.p5) / (2 * _Z_90), 2)),
            0.0,
        ).label("sum_var"),
        func.count(cte.c.id).label("event_count"),
    ).where(cte.c.rn == 1)

    row = db.execute(stmt).first()
    facility_count = db.execute(select(func.count()).select_from(fa)).scalar_one()

    agg_std = math.sqrt(row.sum_var)
    agg_p50 = row.p50
    agg_p5 = max(0.0, agg_p50 - _Z_90 * agg_std)
    agg_p95 = agg_p50 + _Z_90 * agg_std

    return HeadlineResponse(
        generated_at=_now(),
        total_p5=agg_p5,
        total_p50=agg_p50,
        total_p95=agg_p95,
        confirmed_event_count=row.event_count,
        facility_count=facility_count,
    )


@router.get("/by_facility_type", response_model=AggregatesResponse)
def by_facility_type(
    db: DbSession,
    methodology_version: str | None = Query(None, description="Filter to a specific methodology version"),
) -> AggregatesResponse:
    """Aggregate published emissions grouped by facility type (latest estimate per event)."""
    fe = models.fire_events
    fa = models.facilities
    cte = _latest_estimates_cte(methodology_version)

    stmt = (
        select(
            fa.c.facility_type.label("key"),
            func.sum(cte.c.p50).label("p50"),
            func.sum(func.pow((cte.c.p95 - cte.c.p5) / (2 * _Z_90), 2)).label("sum_var"),
            func.count(cte.c.id).label("event_count"),
        )
        .select_from(
            cte.join(fe, cte.c.event_id == fe.c.id)
            .join(fa, fe.c.facility_id == fa.c.id)
        )
        .where(cte.c.rn == 1)
        .group_by(fa.c.facility_type)
        .order_by(fa.c.facility_type)
    )
    rows = db.execute(stmt).all()
    data = []
    for r in rows:
        s = math.sqrt(r.sum_var)
        data.append(AggregateRow(
            key=r.key,
            p5=max(0.0, r.p50 - _Z_90 * s),
            p50=r.p50,
            p95=r.p50 + _Z_90 * s,
            event_count=r.event_count,
        ))
    return AggregatesResponse(generated_at=_now(), data=data)


@router.get("/by_country", response_model=AggregatesResponse)
def by_country(
    db: DbSession,
    methodology_version: str | None = Query(None, description="Filter to a specific methodology version"),
) -> AggregatesResponse:
    """Aggregate published emissions grouped by country (latest estimate per event)."""
    fe = models.fire_events
    fa = models.facilities
    cte = _latest_estimates_cte(methodology_version)

    stmt = (
        select(
            fa.c.country.label("key"),
            func.sum(cte.c.p50).label("p50"),
            func.sum(func.pow((cte.c.p95 - cte.c.p5) / (2 * _Z_90), 2)).label("sum_var"),
            func.count(cte.c.id).label("event_count"),
        )
        .select_from(
            cte.join(fe, cte.c.event_id == fe.c.id)
            .join(fa, fe.c.facility_id == fa.c.id)
        )
        .where(cte.c.rn == 1)
        .group_by(fa.c.country)
        .order_by(fa.c.country)
    )
    rows = db.execute(stmt).all()
    data = []
    for r in rows:
        s = math.sqrt(r.sum_var)
        data.append(AggregateRow(
            key=r.key,
            p5=max(0.0, r.p50 - _Z_90 * s),
            p50=r.p50,
            p95=r.p50 + _Z_90 * s,
            event_count=r.event_count,
        ))
    return AggregatesResponse(generated_at=_now(), data=data)
