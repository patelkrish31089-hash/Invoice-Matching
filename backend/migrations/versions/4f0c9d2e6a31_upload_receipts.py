"""Persist upload intents for crash-safe recovery.

Revision ID: 4f0c9d2e6a31
Revises: 8d2f43b1c76a
Create Date: 2026-10-15
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4f0c9d2e6a31"
down_revision: Union[str, None] = "8d2f43b1c76a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "upload_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("source_reference", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("staging_path", sa.String(length=600), nullable=False),
        sa.Column("stored_path", sa.String(length=600), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("invoice_id", sa.String(length=36), nullable=True),
        sa.Column("last_error_type", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "idempotency_key_hash", name="uq_upload_receipt_actor_key_hash"),
        sa.UniqueConstraint("source", "source_reference", name="uq_upload_receipt_source_ref"),
    )
    op.create_index("ix_upload_receipts_state", "upload_receipts", ["state"], unique=False)
    op.create_index("ix_upload_receipt_state_updated", "upload_receipts", ["state", "updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_upload_receipt_state_updated", table_name="upload_receipts")
    op.drop_index("ix_upload_receipts_state", table_name="upload_receipts")
    op.drop_table("upload_receipts")
