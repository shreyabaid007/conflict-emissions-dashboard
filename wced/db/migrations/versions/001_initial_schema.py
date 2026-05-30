"""Initial schema — all WCED tables.

Revision ID: 001
Revises: None
Create Date: 2026-05-23
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # -- facilities --
    op.create_table(
        "facilities",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("facility_type", sa.Text, nullable=False),
        sa.Column("geometry", sa.Text, nullable=False),
        sa.Column("country", sa.String(3), nullable=False),
        sa.Column("capacity_barrels", sa.Float, nullable=True),
        sa.Column("capacity_uncertainty_pct", sa.Float, nullable=False, server_default="30.0"),
        sa.Column("operator", sa.Text, nullable=True),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.execute(
        "ALTER TABLE facilities ALTER COLUMN geometry "
        "TYPE geometry(Geometry, 4326) USING ST_GeomFromText(geometry, 4326)"
    )
    op.create_index("ix_facilities_geometry", "facilities", ["geometry"], postgresql_using="gist")
    op.create_index("ix_facilities_country", "facilities", ["country"])

    # -- fire_events --
    op.create_table(
        "fire_events",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "facility_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("facilities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("peak_frp_mw", sa.Float, nullable=False),
        sa.Column("total_frp_integral_mj", sa.Float, nullable=True),
        sa.Column("detection_source", sa.Text, nullable=False),
        sa.Column("confidence_label", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("provenance_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_fire_events_facility_id", "fire_events", ["facility_id"])
    op.create_index("ix_fire_events_detected_at", "fire_events", ["detected_at"])
    op.create_index("ix_fire_events_status", "fire_events", ["status"])

    # -- editorial_actions --
    op.create_table(
        "editorial_actions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("reviewer", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("previous_status", sa.Text, nullable=False),
        sa.Column("new_status", sa.Text, nullable=False),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_editorial_actions_event_id", "editorial_actions", ["event_id"])

    # -- firms_detections --
    op.create_table(
        "firms_detections",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("brightness", sa.Float, nullable=True),
        sa.Column("frp", sa.Float, nullable=True),
        sa.Column("confidence", sa.Text, nullable=True),
        sa.Column("acq_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("satellite", sa.Text, nullable=False),
        sa.Column("instrument", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=True),
        sa.Column("bright_t31", sa.Float, nullable=True),
        sa.Column("scan", sa.Float, nullable=True),
        sa.Column("track", sa.Float, nullable=True),
        sa.Column("raw_json", JSONB, nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_firms_detections_acq_datetime", "firms_detections", ["acq_datetime"])

    # -- s2_chips --
    op.create_table(
        "s2_chips",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "facility_id", sa.UUID(as_uuid=True),
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
    op.create_index("ix_s2_chips_event_id", "s2_chips", ["event_id"])

    # -- acled_events --
    op.create_table(
        "acled_events",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("acled_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("event_date", sa.Date, nullable=False),
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
    op.create_index("ix_acled_events_event_date", "acled_events", ["event_date"])

    # -- damage_assessments --
    op.create_table(
        "damage_assessments",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "facility_id", sa.UUID(as_uuid=True),
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
    op.create_index("ix_damage_assessments_event_id", "damage_assessments", ["event_id"])

    # -- emission_estimates --
    op.create_table(
        "emission_estimates",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("methodology_version", sa.Text, nullable=False),
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
    op.create_index("ix_emission_estimates_event_id", "emission_estimates", ["event_id"])
    op.create_index("ix_emission_estimates_methodology", "emission_estimates", ["methodology_version"])
    op.create_index(
        "ix_emission_estimates_event_method",
        "emission_estimates",
        ["event_id", "methodology_version"],
    )

    # -- provenance_records --
    op.create_table(
        "provenance_records",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("produced_by", sa.Text, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("parameters", JSONB, nullable=False, server_default="{}"),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence_label", sa.Text, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_provenance_records_produced_at", "provenance_records", ["produced_at"])
    op.create_index("ix_provenance_records_produced_by", "provenance_records", ["produced_by"])

    # -- sources --
    op.create_table(
        "sources",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("identifier", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("metadata_", JSONB, nullable=False, server_default="{}"),
    )

    # -- provenance_inputs --
    op.create_table(
        "provenance_inputs",
        sa.Column(
            "provenance_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("provenance_records.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("input_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("input_type", sa.Text, nullable=False),
    )

    # -- validation_reports --
    op.create_table(
        "validation_reports",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("fire_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tropomi_estimate_p50", sa.Float, nullable=False),
        sa.Column("discrepancy_ratio", sa.Float, nullable=False),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_validation_reports_event_id", "validation_reports", ["event_id"])

    # -- methodology_versions --
    op.create_table(
        "methodology_versions",
        sa.Column("version_id", sa.Text, primary_key=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pdf_url", sa.Text, nullable=False),
        sa.Column("changelog", sa.Text, nullable=True),
    )

    # -- pipeline_runs --
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("flow_name", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("metrics", JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("ix_pipeline_runs_flow_name", "pipeline_runs", ["flow_name"])


def downgrade() -> None:
    op.drop_table("pipeline_runs")
    op.drop_table("methodology_versions")
    op.drop_table("validation_reports")
    op.drop_table("provenance_inputs")
    op.drop_table("sources")
    op.drop_table("provenance_records")
    op.drop_table("emission_estimates")
    op.drop_table("damage_assessments")
    op.drop_table("acled_events")
    op.drop_table("s2_chips")
    op.drop_table("firms_detections")
    op.drop_table("editorial_actions")
    op.drop_table("fire_events")
    op.drop_table("facilities")
    op.execute("DROP EXTENSION IF EXISTS postgis")
