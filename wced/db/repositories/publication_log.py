"""Publication log repository — append-only."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from wced.db import models


class PublicationLogRepository:
    """Append-only audit log for publish/retract/restate transitions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        id: UUID,
        target_type: str,
        target_id: UUID,
        from_state: str,
        to_state: str,
        action: str,
        actor: str,
        reason: str | None,
        methodology_version: str | None,
        created_at: datetime,
    ) -> UUID:
        self._session.execute(
            models.publication_log.insert().values(
                id=id,
                target_type=target_type,
                target_id=target_id,
                from_state=from_state,
                to_state=to_state,
                action=action,
                actor=actor,
                reason=reason,
                methodology_version=methodology_version,
                created_at=created_at,
            )
        )
        self._session.flush()
        return id

    def list_by_target(self, target_id: UUID) -> list[dict]:
        rows = self._session.execute(
            select(models.publication_log)
            .where(models.publication_log.c.target_id == target_id)
            .order_by(models.publication_log.c.created_at.asc())
        ).all()
        return [r._asdict() for r in rows]

    def list_recent(self, *, limit: int = 50) -> list[dict]:
        rows = self._session.execute(
            select(models.publication_log)
            .order_by(models.publication_log.c.created_at.desc())
            .limit(limit)
        ).all()
        return [r._asdict() for r in rows]
