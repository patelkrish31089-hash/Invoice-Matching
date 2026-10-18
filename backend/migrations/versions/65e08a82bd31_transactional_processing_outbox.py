"""add durable processing dispatch outbox

Revision ID: 65e08a82bd31
Revises: 2ea3f64b93bd
Create Date: 2026-10-14
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "65e08a82bd31"
down_revision: Union[str, None] = "2ea3f64b93bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "processing_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("invoice_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "event_type", name="uq_processing_outbox_job_event"),
    )
    op.create_index("ix_processing_outbox_invoice_id", "processing_outbox", ["invoice_id"], unique=False)
    op.create_index("ix_processing_outbox_state", "processing_outbox", ["state"], unique=False)
    op.create_index("ix_processing_outbox_available_at", "processing_outbox", ["available_at"], unique=False)
    op.create_index("ix_processing_outbox_due", "processing_outbox", ["state", "available_at"], unique=False)

    # Preserve queued work created before the outbox existed. Delivery is at-least-once;
    # worker job claims fence duplicate tasks. Non-queued historical jobs are not replayed.
    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    queued_jobs = connection.execute(sa.text(
        "SELECT id, invoice_id FROM processing_jobs WHERE state = 'queued'"
    )).mappings().all()
    outbox = sa.table(
        "processing_outbox",
        sa.column("id", sa.String(length=36)),
        sa.column("job_id", sa.String(length=36)),
        sa.column("invoice_id", sa.String(length=36)),
        sa.column("actor_id", sa.String(length=36)),
        sa.column("event_type", sa.String(length=60)),
        sa.column("state", sa.String(length=24)),
        sa.column("attempts", sa.Integer()),
        sa.column("available_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for job in queued_jobs:
        connection.execute(sa.insert(outbox).values(
            id=str(uuid.uuid4()), job_id=job["id"], invoice_id=job["invoice_id"],
            actor_id=None, event_type="process_invoice", state="pending", attempts=0,
            available_at=now, created_at=now,
        ))


def downgrade() -> None:
    op.drop_index("ix_processing_outbox_due", table_name="processing_outbox")
    op.drop_index("ix_processing_outbox_available_at", table_name="processing_outbox")
    op.drop_index("ix_processing_outbox_state", table_name="processing_outbox")
    op.drop_index("ix_processing_outbox_invoice_id", table_name="processing_outbox")
    op.drop_table("processing_outbox")
