"""Recompute runs repository."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from wced.db import models


class RecomputeRunRepository:
    """Track recompute invocations (open at start, close at finish)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def open_run(
        self,
        *,
        id: UUID,
        methodology_version: str,
        date_range_start: datetime | None,
        date_range_end: datetime | None,
        initiator: str,
        trigger: str,
        started_at: datetime,
    ) -> UUID:
        self._session.execute(
            models.recompute_runs.insert().values(
                id=id,
                methodology_version=methodology_version,
                date_range_start=date_range_start,
                date_range_end=date_range_end,
                initiator=initiator,
                trigger=trigger,
                events_affected=None,
                started_at=started_at,
                finished_at=None,
                status="RUNNING",
            )
        )
        self._session.flush()
        return id

    def close_run(
        self,
        run_id: UUID,
        *,
        status: str,
        finished_at: datetime,
        events_affected: int,
    ) -> None:
        self._session.execute(
            models.recompute_runs.update()
            .where(models.recompute_runs.c.id == run_id)
            .values(
                status=status,
                finished_at=finished_at,
                events_affected=events_affected,
            )
        )
        self._session.flush()

    def get(self, run_id: UUID) -> dict | None:
        row = self._session.execute(
            select(models.recompute_runs)
            .where(models.recompute_runs.c.id == run_id)
        ).first()
        return row._asdict() if row else None

    def list_recent(self, *, limit: int = 20) -> list[dict]:
        rows = self._session.execute(
            select(models.recompute_runs)
            .order_by(models.recompute_runs.c.started_at.desc())
            .limit(limit)
        ).all()
        return [r._asdict() for r in rows]
