"""Public revision-log endpoint.

``GET /v1/revisions`` exposes the append-only ``publication_log`` so the
dashboard can show every publish / retract / restate / anomaly-retract
transition. Retractions and restatements are surfaced, never silently deleted
(CLAUDE.md §"Editorial Workflow"; v2 gap 1.5).

The log is the source of truth for the frontend revision view. Each entry
carries a derived ``public_note`` — the public "under review" flag set when
anomaly-watch auto-retracts an estimate (CLAUDE.md gate #5).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from wced.api.dependencies import DbSession
from wced.api.schemas.responses import (
    PaginationMeta,
    RevisionEntry,
    RevisionLogResponse,
)
from wced.db import models

router = APIRouter(prefix="/v1/revisions", tags=["revisions"])

# Actions that auto-retract a published estimate to PENDING_REVIEW carry a
# public "under review" note so readers know a number is being re-checked.
_UNDER_REVIEW_ACTIONS = frozenset({"anomaly_retract"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public_note(action: str, to_state: str) -> str | None:
    """Derive the public-facing note for a publication_log row.

    The durable signal is the row's ``action``; this maps it to reader-facing
    copy. Anomaly auto-retractions are publicly flagged "under review"; all
    other transitions speak for themselves via ``action``/``reason``.
    """
    if action in _UNDER_REVIEW_ACTIONS:
        return "under review"
    return None


@router.get(
    "",
    response_model=RevisionLogResponse,
    responses={422: {"description": "Validation error"}},
)
def list_revisions(
    db: DbSession,
    target_id: UUID | None = Query(
        None, description="Filter to a single event/estimate's history."
    ),
    page: int = Query(1, ge=1, le=10000),
    per_page: int = Query(50, ge=1, le=200),
) -> RevisionLogResponse:
    """Paginated public revision log, newest first.

    Sourced from the append-only ``publication_log`` table. Pass ``target_id``
    to scope the log to one event's full transition history.
    """
    pl = models.publication_log

    where = []
    if target_id is not None:
        where.append(pl.c.target_id == target_id)

    count_stmt = select(func.count()).select_from(pl)
    for clause in where:
        count_stmt = count_stmt.where(clause)
    total = db.execute(count_stmt).scalar_one()

    stmt = select(pl)
    for clause in where:
        stmt = stmt.where(clause)
    stmt = (
        stmt.order_by(pl.c.created_at.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )

    rows = db.execute(stmt).all()
    entries: list[RevisionEntry] = []
    for row in rows:
        d = row._asdict()
        entries.append(RevisionEntry(
            id=d["id"],
            target_type=d["target_type"],
            target_id=d["target_id"],
            from_state=d["from_state"],
            to_state=d["to_state"],
            action=d["action"],
            actor=d["actor"],
            reason=d.get("reason"),
            public_note=_public_note(d["action"], d["to_state"]),
            methodology_version=d.get("methodology_version"),
            created_at=d["created_at"],
        ))

    pages = (total + per_page - 1) // per_page if total else 0
    return RevisionLogResponse(
        generated_at=_now(),
        data=entries,
        pagination=PaginationMeta(
            total=total, page=page, per_page=per_page, pages=pages
        ),
    )
