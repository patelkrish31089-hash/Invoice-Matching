"""Persist idempotency metadata for signed email-ingestion webhook events.

Revision ID: 0e7a2cb91fd4
Revises: d170fe32a6b1
Create Date: 2026-10-15
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0e7a2cb91fd4"
down_revision: Union[str, None] = "d170fe32a6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_ingestion_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_event_id", sa.String(length=160), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_event_id", name="uq_email_ingestion_events_provider_event_id"),
    )


def downgrade() -> None:
    op.drop_table("email_ingestion_events")
