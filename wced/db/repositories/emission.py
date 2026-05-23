"""Emission estimate repository."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from wced.db import models


class EmissionEstimateRepository:
    """CRUD operations for the emission_estimates table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, *, id: UUID, event_id: UUID, methodology_version: str,
               method: str, p5: float, p50: float, p95: float,
               samples_ref: str | None, units: str, provenance_id: UUID,
               parameter_versions: dict[str, Any],
               created_at: datetime) -> UUID:
        """Insert an emission estimate row."""
        self._session.execute(
            models.emission_estimates.insert().values(
                id=id, event_id=event_id,
                methodology_version=methodology_version, method=method,
                p5=p5, p50=p50, p95=p95, samples_ref=samples_ref,
                units=units, provenance_id=provenance_id,
                parameter_versions=parameter_versions,
                created_at=created_at,
            )
        )
        self._session.flush()
        return id

    def get(self, estimate_id: UUID) -> dict | None:
        """Return an estimate as a dict, or None."""
        row = self._session.execute(
            select(models.emission_estimates)
            .where(models.emission_estimates.c.id == estimate_id)
        ).first()
        return row._asdict() if row else None

    def list_by_event(self, event_id: UUID) -> list[dict]:
        """Return all estimates for an event, newest first."""
        rows = self._session.execute(
            select(models.emission_estimates)
            .where(models.emission_estimates.c.event_id == event_id)
            .order_by(models.emission_estimates.c.created_at.desc())
        ).all()
        return [r._asdict() for r in rows]

    def list_by_methodology(self, version: str) -> list[dict]:
        """Return all estimates computed with a specific methodology version."""
        rows = self._session.execute(
            select(models.emission_estimates)
            .where(models.emission_estimates.c.methodology_version == version)
            .order_by(models.emission_estimates.c.created_at.desc())
        ).all()
        return [r._asdict() for r in rows]
