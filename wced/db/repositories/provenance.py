"""Provenance records and sources repository."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from wced.db import models


class ProvenanceRepository:
    """CRUD for provenance_records, sources, and provenance_inputs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert_record(self, *, id: UUID, produced_by: str, method: str,
                      parameters: dict[str, Any], produced_at: datetime,
                      confidence_label: str,
                      notes: str | None = None) -> UUID:
        """Insert a provenance record."""
        self._session.execute(
            models.provenance_records.insert().values(
                id=id, produced_by=produced_by, method=method,
                parameters=parameters, produced_at=produced_at,
                confidence_label=confidence_label, notes=notes,
            )
        )
        self._session.flush()
        return id

    def insert_source(self, *, id: UUID, source_type: str, identifier: str,
                      retrieved_at: datetime, content_hash: str,
                      metadata: dict[str, Any] | None = None) -> UUID:
        """Insert a source row."""
        self._session.execute(
            models.sources.insert().values(
                id=id, source_type=source_type, identifier=identifier,
                retrieved_at=retrieved_at, content_hash=content_hash,
                metadata_=metadata or {},
            )
        )
        self._session.flush()
        return id

    def link_input(self, provenance_id: UUID, input_id: UUID,
                   input_type: str) -> None:
        """Add a provenance_inputs row linking a record to an input."""
        self._session.execute(
            models.provenance_inputs.insert().values(
                provenance_id=provenance_id,
                input_id=input_id,
                input_type=input_type,
            )
        )
        self._session.flush()

    def get_record(self, record_id: UUID) -> dict | None:
        """Fetch a provenance record by id."""
        row = self._session.execute(
            select(models.provenance_records)
            .where(models.provenance_records.c.id == record_id)
        ).first()
        return row._asdict() if row else None

    def get_source(self, source_id: UUID) -> dict | None:
        """Fetch a source by id."""
        row = self._session.execute(
            select(models.sources)
            .where(models.sources.c.id == source_id)
        ).first()
        return row._asdict() if row else None

    def get_inputs(self, provenance_id: UUID) -> list[dict]:
        """Return all inputs for a provenance record."""
        rows = self._session.execute(
            select(models.provenance_inputs)
            .where(models.provenance_inputs.c.provenance_id == provenance_id)
        ).all()
        return [r._asdict() for r in rows]
