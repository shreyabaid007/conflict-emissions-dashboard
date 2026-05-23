"""SQLAlchemy ORM table definitions for the WCED database.

This module defines Core/ORM metadata objects. The actual engine and session
are wired up by the database prompt; these models can be imported independently
for Alembic autogenerate and schema introspection.

Table inventory:
  ``fire_events``        — persistent ``FireEvent`` rows with current status.
  ``editorial_actions``  — append-only audit log of every status transition.

Both tables are intentionally narrower than their Pydantic counterparts: only
the columns needed for querying and joining are stored as native SQL columns;
rich nested structures (e.g. ``hotspots`` tuple) are stored as JSONB.

SQLAlchemy mapping notes
------------------------
- ``UUID`` columns use ``sqlalchemy.UUID(as_uuid=True)`` so Python UUIDs round-
  trip without string conversion.
- ``TIMESTAMP WITH TIME ZONE`` is used for all datetime columns (PostgreSQL
  ``TIMESTAMPTZ``); SQLAlchemy maps this to Python ``datetime`` with tzinfo.
- No foreign-key constraints to the provenance tables in this module — the
  provenance DAG is managed by ``InMemoryProvenanceStore`` /
  ``PostgresProvenanceStore`` and referenced by UUID only.
"""
from __future__ import annotations

try:
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
    _SA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SA_AVAILABLE = False

if _SA_AVAILABLE:
    metadata = sa.MetaData()

    fire_events = sa.Table(
        "fire_events",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("facility_id", sa.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("peak_frp_mw", sa.Float, nullable=False),
        sa.Column("total_frp_integral_mj", sa.Float, nullable=True),
        sa.Column("detection_source", sa.Text, nullable=False),
        sa.Column("confidence_label", sa.Text, nullable=False),
        # Denormalised current status — always derivable from editorial_actions
        # but stored here for O(1) queue queries.
        sa.Column("status", sa.Text, nullable=False, index=True),
        sa.Column("provenance_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
    )

    editorial_actions = sa.Table(
        "editorial_actions",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("reviewer", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("previous_status", sa.Text, nullable=False),
        sa.Column("new_status", sa.Text, nullable=False),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=False),
    )

else:
    # Sentinel so callers can guard: ``if wced.db.models.metadata is None``.
    metadata = None  # type: ignore[assignment]
    fire_events = None  # type: ignore[assignment]
    editorial_actions = None  # type: ignore[assignment]
