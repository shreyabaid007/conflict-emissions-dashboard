"""Damage assessment repository."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from wced.db import models


class DamageAssessmentRepository:
    """CRUD for the damage_assessments table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, *, id: UUID, event_id: UUID, facility_id: UUID,
               fraction_destroyed_low: float, fraction_destroyed_mode: float,
               fraction_destroyed_high: float, assessed_by: str,
               assessment_method: str, notes: str | None,
               assessed_at: datetime, provenance_id: UUID) -> UUID:
        """Insert a damage assessment row."""
        self._session.execute(
            models.damage_assessments.insert().values(
                id=id, event_id=event_id, facility_id=facility_id,
                fraction_destroyed_low=fraction_destroyed_low,
                fraction_destroyed_mode=fraction_destroyed_mode,
                fraction_destroyed_high=fraction_destroyed_high,
                assessed_by=assessed_by,
                assessment_method=assessment_method,
                notes=notes, assessed_at=assessed_at,
                provenance_id=provenance_id,
            )
        )
        self._session.flush()
        return id

    def get(self, assessment_id: UUID) -> dict | None:
        """Fetch a damage assessment by id."""
        row = self._session.execute(
            select(models.damage_assessments)
            .where(models.damage_assessments.c.id == assessment_id)
        ).first()
        return row._asdict() if row else None

    def list_by_event(self, event_id: UUID) -> list[dict]:
        """Return all assessments for an event."""
        rows = self._session.execute(
            select(models.damage_assessments)
            .where(models.damage_assessments.c.event_id == event_id)
            .order_by(models.damage_assessments.c.assessed_at.desc())
        ).all()
        return [r._asdict() for r in rows]
