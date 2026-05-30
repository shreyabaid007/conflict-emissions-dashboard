"""Pipeline run repository."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from wced.db import models


class PipelineRunRepository:
    """Track pipeline execution runs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, *, id: UUID, flow_name: str, started_at: datetime,
               status: str, ended_at: datetime | None = None,
               metrics: dict[str, Any] | None = None) -> UUID:
        """Record a new pipeline run."""
        self._session.execute(
            models.pipeline_runs.insert().values(
                id=id, flow_name=flow_name, started_at=started_at,
                ended_at=ended_at, status=status, metrics=metrics or {},
            )
        )
        self._session.flush()
        return id

    def finish(self, run_id: UUID, *, status: str, ended_at: datetime,
               metrics: dict[str, Any] | None = None) -> None:
        """Mark a pipeline run as finished."""
        values: dict[str, Any] = {"status": status, "ended_at": ended_at}
        if metrics is not None:
            values["metrics"] = metrics
        self._session.execute(
            models.pipeline_runs.update()
            .where(models.pipeline_runs.c.id == run_id)
            .values(**values)
        )
        self._session.flush()

    def get(self, run_id: UUID) -> dict | None:
        """Fetch a pipeline run by id."""
        row = self._session.execute(
            select(models.pipeline_runs)
            .where(models.pipeline_runs.c.id == run_id)
        ).first()
        return row._asdict() if row else None

    def list_recent(self, flow_name: str | None = None, *,
                    limit: int = 20) -> list[dict]:
        """Return recent pipeline runs, optionally filtered by flow."""
        stmt = select(models.pipeline_runs)
        if flow_name is not None:
            stmt = stmt.where(models.pipeline_runs.c.flow_name == flow_name)
        stmt = stmt.order_by(models.pipeline_runs.c.started_at.desc()).limit(limit)
        rows = self._session.execute(stmt).all()
        return [r._asdict() for r in rows]
