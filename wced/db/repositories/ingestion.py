"""Repositories for raw ingestion tables (FIRMS, ACLED, S2 chips)."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from wced.db import models


class FirmsDetectionRepository:
    """Batch insert and query raw FIRMS hotspot detections."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert_batch(self, rows: list[dict[str, Any]]) -> int:
        """Insert a batch of FIRMS detection dicts. Returns count inserted."""
        if not rows:
            return 0
        self._session.execute(models.firms_detections.insert(), rows)
        self._session.flush()
        return len(rows)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(models.firms_detections)
        ).scalar_one()

    def list_by_time_range(self, start: datetime, end: datetime,
                           *, limit: int = 1000) -> list[dict]:
        """Return detections within a time range."""
        rows = self._session.execute(
            select(models.firms_detections)
            .where(models.firms_detections.c.acq_datetime.between(start, end))
            .order_by(models.firms_detections.c.acq_datetime)
            .limit(limit)
        ).all()
        return [r._asdict() for r in rows]


class AcledEventRepository:
    """Upsert and query cached ACLED events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, row: dict[str, Any]) -> UUID:
        """Insert or update an ACLED event by acled_id."""
        stmt = pg_insert(models.acled_events).values(**row).on_conflict_do_update(
            index_elements=["acled_id"],
            set_={k: v for k, v in row.items() if k != "id"},
        )
        self._session.execute(stmt)
        self._session.flush()
        return row["id"]

    def upsert_batch(self, rows: list[dict[str, Any]]) -> int:
        """Upsert a batch of ACLED events. Returns count."""
        for row in rows:
            self.upsert(row)
        return len(rows)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(models.acled_events)
        ).scalar_one()


class S2ChipRepository:
    """Store and query Sentinel-2 chip references."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, row: dict[str, Any]) -> UUID:
        """Insert an S2 chip reference row."""
        self._session.execute(models.s2_chips.insert().values(**row))
        self._session.flush()
        return row["id"]

    def list_by_event(self, event_id: UUID) -> list[dict]:
        """Return all S2 chips associated with an event."""
        rows = self._session.execute(
            select(models.s2_chips)
            .where(models.s2_chips.c.event_id == event_id)
            .order_by(models.s2_chips.c.acquisition_date)
        ).all()
        return [r._asdict() for r in rows]
