"""Methodology, changelog, and health endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, text

from wced.api.dependencies import DbSession
from wced.api.schemas.responses import (
    ChangelogEntry,
    ChangelogResponse,
    HealthResponse,
    MetaResponse,
    MethodologyResponse,
)
from wced.db import models

router = APIRouter(tags=["meta"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/v1/meta", response_model=MetaResponse)
def meta(db: DbSession) -> MetaResponse:
    """Summary metadata: event count, facility count, last update."""
    from sqlalchemy import func as sqlfunc
    fe = models.fire_events
    fa = models.facilities

    event_count = db.execute(select(sqlfunc.count()).select_from(fe)).scalar_one()
    facility_count = db.execute(select(sqlfunc.count()).select_from(fa)).scalar_one()

    last_row = db.execute(
        select(fe.c.detected_at).order_by(fe.c.detected_at.desc()).limit(1)
    ).first()
    last_update = last_row[0].isoformat() if last_row else None

    return MetaResponse(
        event_count=event_count,
        facility_count=facility_count,
        last_data_update=last_update,
    )


@router.get("/v1/methodology/current", response_model=MethodologyResponse, responses={404: {"description": "No methodology version registered"}})
def current_methodology(db: DbSession) -> MethodologyResponse:
    """Return metadata for the current (most recent) methodology version."""
    mv = models.methodology_versions
    row = db.execute(
        select(mv).order_by(mv.c.released_at.desc()).limit(1)
    ).first()
    if row is None:
        raise HTTPException(404, detail="No methodology version registered")
    d = row._asdict()
    return MethodologyResponse(
        generated_at=_now(),
        version_id=d["version_id"],
        released_at=d["released_at"],
        pdf_url=d["pdf_url"],
    )


@router.get("/v1/changelog", response_model=ChangelogResponse)
def changelog(db: DbSession) -> ChangelogResponse:
    """Methodology version changes and event retractions."""
    entries: list[ChangelogEntry] = []

    mv = models.methodology_versions
    for row in db.execute(select(mv).order_by(mv.c.released_at.desc())).all():
        d = row._asdict()
        entries.append(ChangelogEntry(
            version_id=d["version_id"],
            change_type="methodology_release",
            detail=d.get("changelog") or f"Released methodology {d['version_id']}",
            occurred_at=d["released_at"],
        ))

    ea = models.editorial_actions
    retractions = db.execute(
        select(ea)
        .where(ea.c.action_type == "RETRACTED")
        .order_by(ea.c.acted_at.desc())
    ).all()
    for row in retractions:
        d = row._asdict()
        entries.append(ChangelogEntry(
            event_id=d["event_id"],
            change_type="event_retraction",
            detail=d.get("notes") or "Event retracted",
            occurred_at=d["acted_at"],
        ))

    entries.sort(key=lambda e: e.occurred_at, reverse=True)
    return ChangelogResponse(generated_at=_now(), entries=entries)


@router.get("/v1/health", response_model=HealthResponse)
def health(db: DbSession) -> HealthResponse:
    """Service health check."""
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "unavailable"
    return HealthResponse(status="ok", database=db_status)
