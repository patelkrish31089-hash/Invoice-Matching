"""Track malware-positive invoice attachments in private quarantine.

Revision ID: 8d2f43b1c76a
Revises: 0e7a2cb91fd4
Create Date: 2026-10-15
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8d2f43b1c76a"
down_revision: Union[str, None] = "0e7a2cb91fd4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "quarantined_uploads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("source_reference", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("quarantine_path", sa.String(length=600), nullable=False),
        sa.Column("scanner_name", sa.String(length=80), nullable=False),
        sa.Column("scanner_version", sa.String(length=160), nullable=True),
        sa.Column("detection", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_id", sa.String(length=36), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_reference", name="uq_quarantined_upload_source_ref"),
    )
    op.create_index("ix_quarantined_uploads_sha256", "quarantined_uploads", ["sha256"], unique=False)
    op.create_index("ix_quarantined_uploads_status", "quarantined_uploads", ["status"], unique=False)
    op.create_index("ix_quarantined_upload_status_created", "quarantined_uploads", ["status", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_quarantined_upload_status_created", table_name="quarantined_uploads")
    op.drop_index("ix_quarantined_uploads_status", table_name="quarantined_uploads")
    op.drop_index("ix_quarantined_uploads_sha256", table_name="quarantined_uploads")
    op.drop_table("quarantined_uploads")
