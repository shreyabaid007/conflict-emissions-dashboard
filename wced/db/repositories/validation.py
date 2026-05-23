"""Validation report repository."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from wced.db import models


class ValidationReportRepository:
    """CRUD for validation_reports table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, *, id: UUID, event_id: UUID, tropomi_estimate_p50: float,
               discrepancy_ratio: float, needs_review: bool,
               generated_at: datetime) -> UUID:
        """Insert a validation report row."""
        self._session.execute(
            models.validation_reports.insert().values(
                id=id, event_id=event_id,
                tropomi_estimate_p50=tropomi_estimate_p50,
                discrepancy_ratio=discrepancy_ratio,
                needs_review=needs_review, generated_at=generated_at,
            )
        )
        self._session.flush()
        return id

    def list_by_event(self, event_id: UUID) -> list[dict]:
        """Return all validation reports for an event."""
        rows = self._session.execute(
            select(models.validation_reports)
            .where(models.validation_reports.c.event_id == event_id)
            .order_by(models.validation_reports.c.generated_at.desc())
        ).all()
        return [r._asdict() for r in rows]

    def list_needing_review(self, *, limit: int = 50) -> list[dict]:
        """Return reports flagged as needing review."""
        rows = self._session.execute(
            select(models.validation_reports)
            .where(models.validation_reports.c.needs_review.is_(True))
            .order_by(models.validation_reports.c.generated_at.desc())
            .limit(limit)
        ).all()
        return [r._asdict() for r in rows]
