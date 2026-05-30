"""SQLAlchemy Core table definitions for the WCED database.

This module defines Core metadata objects. The actual engine and session
are wired up by ``wced.db.session``; these models can be imported independently
for Alembic autogenerate and schema introspection.

SQLAlchemy mapping notes
------------------------
- ``UUID`` columns use ``sa.UUID(as_uuid=True)`` so Python UUIDs round-trip
  without string conversion.
- ``TIMESTAMP WITH TIME ZONE`` is used for all datetime columns (PostgreSQL
  ``TIMESTAMPTZ``); SQLAlchemy maps this to Python ``datetime`` with tzinfo.
- PostGIS geometry columns use ``geoalchemy2.Geometry`` when available,
  falling back to a plain ``Text`` column if geoalchemy2 is not installed
  (unit tests that don't touch spatial queries).
"""
from __future__ import annotations

try:
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import JSONB

    _SA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SA_AVAILABLE = False

try:
    from geoalchemy2 import Geometry

    _GEO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _GEO_AVAILABLE = False

if _SA_AVAILABLE:
    metadata = sa.MetaData()

    _geom_col_type = Geometry("GEOMETRY", srid=4326) if _GEO_AVAILABLE else sa.Text

    # ------------------------------------------------------------------
    # facilities
    # ------------------------------------------------------------------
    facilities = sa.Table(
        "facilities",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("facility_type", sa.Text, nullable=False),
        sa.Column("geometry", _geom_col_type, nullable=False),
        sa.Column("country", sa.String(3), nullable=False),
        sa.Column("capacity_barrels", sa.Float, nullable=True),
        sa.Column("capacity_uncertainty_pct", sa.Float, nullable=False, server_default="30.0"),
        sa.Column("operator", sa.Text, nullable=True),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
    )

    # ------------------------------------------------------------------
    # fire_events
    # ------------------------------------------------------------------
    fire_events = sa.Table(
        "fire_events",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "facility_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("facilities.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("peak_frp_mw", sa.Float, nullable=False),
        sa.Column("total_frp_integral_mj", sa.Float, nullable=True),
        sa.Column("detection_source", sa.Text, nullable=False),
        sa.Column("confidence_label", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, index=True),
        sa.Column("provenance_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
    )

    # ------------------------------------------------------------------
    # editorial_actions (append-only audit log)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # firms_detections — raw FIRMS hotspot rows (one per detection, for replay)
    # ------------------------------------------------------------------
    firms_detections = sa.Table(
        "firms_detections",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("brightness", sa.Float, nullable=True),
        sa.Column("frp", sa.Float, nullable=True),
        sa.Column("confidence", sa.Text, nullable=True),
        sa.Column("acq_datetime", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("satellite", sa.Text, nullable=False),
        sa.Column("instrument", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=True),
        sa.Column("bright_t31", sa.Float, nullable=True),
        sa.Column("scan", sa.Float, nullable=True),
        sa.Column("track", sa.Float, nullable=True),
        sa.Column("raw_json", JSONB, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # s2_chips — storage references for fetched Sentinel-2 chips
    # ------------------------------------------------------------------
    s2_chips = sa.Table(
        "s2_chips",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "facility_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("facilities.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("product_id", sa.Text, nullable=False),
        sa.Column("acquisition_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cloud_cover_pct", sa.Float, nullable=True),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("bands", JSONB, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # acled_events — cached ACLED events
    # ------------------------------------------------------------------
    acled_events = sa.Table(
        "acled_events",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("acled_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("event_date", sa.Date, nullable=False, index=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("sub_event_type", sa.Text, nullable=True),
        sa.Column("country", sa.Text, nullable=False),
        sa.Column("admin1", sa.Text, nullable=True),
        sa.Column("admin2", sa.Text, nullable=True),
        sa.Column("location", sa.Text, nullable=True),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("raw_json", JSONB, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # damage_assessments
    # ------------------------------------------------------------------
    damage_assessments = sa.Table(
        "damage_assessments",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "facility_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("facilities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fraction_destroyed_low", sa.Float, nullable=False),
        sa.Column("fraction_destroyed_mode", sa.Float, nullable=False),
        sa.Column("fraction_destroyed_high", sa.Float, nullable=False),
        sa.Column("assessed_by", sa.Text, nullable=False),
        sa.Column("assessment_method", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_id", sa.UUID(as_uuid=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # emission_estimates
    # ------------------------------------------------------------------
    emission_estimates = sa.Table(
        "emission_estimates",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("methodology_version", sa.Text, nullable=False, index=True),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("p5", sa.Float, nullable=False),
        sa.Column("p50", sa.Float, nullable=False),
        sa.Column("p95", sa.Float, nullable=False),
        sa.Column("samples_ref", sa.Text, nullable=True),
        sa.Column("units", sa.Text, nullable=False, server_default="tCO2e"),
        sa.Column("provenance_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("parameter_versions", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # provenance_records
    # ------------------------------------------------------------------
    provenance_records = sa.Table(
        "provenance_records",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("produced_by", sa.Text, nullable=False, index=True),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("parameters", JSONB, nullable=False, server_default="{}"),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("confidence_label", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
    )

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------
    sources = sa.Table(
        "sources",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("identifier", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("metadata_", JSONB, nullable=False, server_default="{}"),
    )

    # ------------------------------------------------------------------
    # provenance_inputs — many-to-many between provenance_records and
    # their inputs (sources or other provenance_records)
    # ------------------------------------------------------------------
    provenance_inputs = sa.Table(
        "provenance_inputs",
        metadata,
        sa.Column(
            "provenance_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("provenance_records.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("input_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("input_type", sa.Text, nullable=False),
    )

    # ------------------------------------------------------------------
    # validation_reports
    # ------------------------------------------------------------------
    validation_reports = sa.Table(
        "validation_reports",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("tropomi_estimate_p50", sa.Float, nullable=False),
        sa.Column("discrepancy_ratio", sa.Float, nullable=False),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # methodology_versions
    # ------------------------------------------------------------------
    methodology_versions = sa.Table(
        "methodology_versions",
        metadata,
        sa.Column("version_id", sa.Text, primary_key=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pdf_url", sa.Text, nullable=False),
        sa.Column("changelog", sa.Text, nullable=True),
    )

    # ------------------------------------------------------------------
    # pipeline_runs
    # ------------------------------------------------------------------
    pipeline_runs = sa.Table(
        "pipeline_runs",
        metadata,
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("flow_name", sa.Text, nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("metrics", JSONB, nullable=False, server_default="{}"),
    )

    # ------------------------------------------------------------------
    # Composite indexes (beyond single-column indexes defined inline)
    # ------------------------------------------------------------------
    sa.Index("ix_emission_estimates_event_method", emission_estimates.c.event_id, emission_estimates.c.methodology_version)

else:  # pragma: no cover
    metadata = None  # type: ignore[assignment]
    facilities = None  # type: ignore[assignment]
    fire_events = None  # type: ignore[assignment]
    editorial_actions = None  # type: ignore[assignment]
    firms_detections = None  # type: ignore[assignment]
    s2_chips = None  # type: ignore[assignment]
    acled_events = None  # type: ignore[assignment]
    damage_assessments = None  # type: ignore[assignment]
    emission_estimates = None  # type: ignore[assignment]
    provenance_records = None  # type: ignore[assignment]
    sources = None  # type: ignore[assignment]
    provenance_inputs = None  # type: ignore[assignment]
    validation_reports = None  # type: ignore[assignment]
    methodology_versions = None  # type: ignore[assignment]
    pipeline_runs = None  # type: ignore[assignment]
