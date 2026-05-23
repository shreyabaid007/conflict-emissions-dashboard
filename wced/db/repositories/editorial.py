"""Editorial action repository."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from wced.db import models


class EditorialActionRepository:
    """CRUD for the editorial_actions table (append-only audit log)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, *, id: UUID, event_id: UUID, action_type: str,
               reviewer: str, notes: str | None, previous_status: str,
               new_status: str, acted_at: datetime) -> UUID:
        """Append an editorial action row."""
        self._session.execute(
            models.editorial_actions.insert().values(
                id=id, event_id=event_id, action_type=action_type,
                reviewer=reviewer, notes=notes,
                previous_status=previous_status,
                new_status=new_status, acted_at=acted_at,
            )
        )
        self._session.flush()
        return id

    def list_by_event(self, event_id: UUID) -> list[dict]:
        """Return all editorial actions for an event, oldest first."""
        rows = self._session.execute(
            select(models.editorial_actions)
            .where(models.editorial_actions.c.event_id == event_id)
            .order_by(models.editorial_actions.c.acted_at.asc())
        ).all()
        return [r._asdict() for r in rows]
