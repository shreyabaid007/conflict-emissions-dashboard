"""Timeseries endpoints — daily and cumulative emissions.

Uses only the latest methodology-version estimate per event to avoid
double-counting when estimates are recomputed under a new version.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from wced.api.dependencies import DbSession
from wced.api.schemas.responses import DailyPoint, TimeseriesResponse
from wced.db import models

_Z_90 = 1.6449

router = APIRouter(prefix="/v1/timeseries", tags=["timeseries"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _DailyRaw:
    """Internal row carrying p50 and sum-of-variances for CLT aggregation."""
    __slots__ = ("day", "p50", "sum_var")
    def __init__(self, day: date, p50: float, sum_var: float):
        self.day = day
        self.p50 = p50
        self.sum_var = sum_var


def _daily_query_raw(
    db: "DbSession",
    from_date: date | None,
    to_date: date | None,
    methodology_version: str | None = None,
) -> list[_DailyRaw]:
    fe = models.fire_events
    ee = models.emission_estimates

    rn = func.row_number().over(
        partition_by=ee.c.event_id,
        order_by=[ee.c.methodology_version.desc(), ee.c.created_at.desc()],
    ).label("rn")

    inner_stmt = (
        select(ee, rn, fe.c.detected_at.label("event_detected_at"))
        .join(fe, ee.c.event_id == fe.c.id)
        .where(fe.c.status == "PUBLISHED")
    )
    if methodology_version:
        inner_stmt = inner_stmt.where(ee.c.methodology_version == methodology_version)

    cte = inner_stmt.cte("latest_est")
    day_col = func.date(cte.c.event_detected_at).label("day")

    stmt = (
        select(
            day_col,
            func.sum(cte.c.p50).label("p50"),
            func.sum(func.pow((cte.c.p95 - cte.c.p5) / (2 * _Z_90), 2)).label("sum_var"),
        )
        .where(cte.c.rn == 1)
        .group_by(day_col)
        .order_by(day_col)
    )
    if from_date:
        stmt = stmt.where(
            cte.c.event_detected_at >= datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
        )
    if to_date:
        stmt = stmt.where(
            cte.c.event_detected_at <= datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc)
        )

    rows = db.execute(stmt).all()
    return [_DailyRaw(day=r.day, p50=r.p50, sum_var=r.sum_var) for r in rows]


def _raw_to_daily(raw: list[_DailyRaw]) -> list[DailyPoint]:
    pts: list[DailyPoint] = []
    for r in raw:
        s = math.sqrt(r.sum_var)
        pts.append(DailyPoint(
            date=r.day,
            p5=max(0.0, r.p50 - _Z_90 * s),
            p50=r.p50,
            p95=r.p50 + _Z_90 * s,
        ))
    return pts


@router.get("/daily", response_model=TimeseriesResponse, responses={422: {"description": "Validation error"}})
def daily_emissions(
    db: DbSession,
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    methodology_version: str | None = Query(None, description="Filter to a specific methodology version"),
) -> TimeseriesResponse:
    """Daily emission totals (published events only, latest estimate per event)."""
    raw = _daily_query_raw(db, from_date, to_date, methodology_version)
    return TimeseriesResponse(generated_at=_now(), data=_raw_to_daily(raw))


@router.get("/cumulative", response_model=TimeseriesResponse, responses={422: {"description": "Validation error"}})
def cumulative_emissions(
    db: DbSession,
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    methodology_version: str | None = Query(None, description="Filter to a specific methodology version"),
) -> TimeseriesResponse:
    """Cumulative (running-sum) daily emissions (latest estimate per event).

    Variances are summed (independent events) and percentiles recomputed
    from the cumulative std via CLT, rather than summing individual p5/p95.
    """
    raw = _daily_query_raw(db, from_date, to_date, methodology_version)
    cumulative: list[DailyPoint] = []
    running_p50 = 0.0
    running_var = 0.0
    for r in raw:
        running_p50 += r.p50
        running_var += r.sum_var
        s = math.sqrt(running_var)
        cumulative.append(DailyPoint(
            date=r.day,
            p5=max(0.0, running_p50 - _Z_90 * s),
            p50=running_p50,
            p95=running_p50 + _Z_90 * s,
        ))
    return TimeseriesResponse(generated_at=_now(), data=cumulative)
