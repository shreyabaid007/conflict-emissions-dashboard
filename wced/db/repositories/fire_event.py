"""Fire event repository."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from wced.db import models


class FireEventRepository:
    """CRUD operations for the fire_events table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, *, id: UUID, facility_id: UUID, detected_at: datetime,
               last_seen_at: datetime, peak_frp_mw: float,
               total_frp_integral_mj: float | None, detection_source: str,
               confidence_label: str, status: str, provenance_id: UUID,
               created_at: datetime, updated_at: datetime,
               notes: str | None = None) -> UUID:
        """Insert a fire event row."""
        self._session.execute(
            models.fire_events.insert().values(
                id=id, facility_id=facility_id, detected_at=detected_at,
                last_seen_at=last_seen_at, peak_frp_mw=peak_frp_mw,
                total_frp_integral_mj=total_frp_integral_mj,
                detection_source=detection_source,
                confidence_label=confidence_label, status=status,
                provenance_id=provenance_id, created_at=created_at,
                updated_at=updated_at, notes=notes,
            )
        )
        self._session.flush()
        return id

    def get(self, event_id: UUID) -> dict | None:
        """Return a fire event as a dict, or None if not found."""
        row = self._session.execute(
            select(models.fire_events).where(models.fire_events.c.id == event_id)
        ).first()
        if row is None:
            return None
        return row._asdict()

    def update_status(self, event_id: UUID, status: str, updated_at: datetime) -> None:
        """Update the denormalised status column."""
        self._session.execute(
            models.fire_events.update()
            .where(models.fire_events.c.id == event_id)
            .values(status=status, updated_at=updated_at)
        )
        self._session.flush()

    def list_by_status(self, status: str, *, limit: int = 100) -> list[dict]:
        """Return fire events with the given status."""
        rows = self._session.execute(
            select(models.fire_events)
            .where(models.fire_events.c.status == status)
            .order_by(models.fire_events.c.detected_at.desc())
            .limit(limit)
        ).all()
        return [r._asdict() for r in rows]

    def list_by_facility(self, facility_id: UUID) -> list[dict]:
        """Return all fire events for a facility."""
        rows = self._session.execute(
            select(models.fire_events)
            .where(models.fire_events.c.facility_id == facility_id)
            .order_by(models.fire_events.c.detected_at.desc())
        ).all()
        return [r._asdict() for r in rows]

    def count(self) -> int:
        """Return total number of fire events."""
        return self._session.execute(
            select(func.count()).select_from(models.fire_events)
        ).scalar_one()
