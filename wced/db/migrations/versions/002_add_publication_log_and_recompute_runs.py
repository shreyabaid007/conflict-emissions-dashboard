"""Add publication_log and recompute_runs tables.

Revision ID: 002
Revises: 001
Create Date: 2026-05-30
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "publication_log",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("target_type", sa.Text, nullable=False),
        sa.Column("target_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("from_state", sa.Text, nullable=False),
        sa.Column("to_state", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("methodology_version", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_publication_log_target_id", "publication_log", ["target_id"])

    op.create_table(
        "recompute_runs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("methodology_version", sa.Text, nullable=False),
        sa.Column("date_range_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_range_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initiator", sa.Text, nullable=False),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("events_affected", sa.Integer, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("recompute_runs")
    op.drop_index("ix_publication_log_target_id", table_name="publication_log")
    op.drop_table("publication_log")
