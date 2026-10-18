"""Add non-executable payment proposals and a separately assigned approver role.

Revision ID: a71f3c2e9b40
Revises: c6d2e0a1f7b4
Create Date: 2026-10-17
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a71f3c2e9b40"
down_revision: Union[str, None] = "c6d2e0a1f7b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "payment_proposals"
APPROVER_ROLE = "approver"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("invoice_id", sa.String(length=36), nullable=False),
        sa.Column("review_decision_id", sa.String(length=36), nullable=True),
        sa.Column("vendor_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("invoice_number", sa.String(length=100), nullable=False),
        sa.Column("vendor_code", sa.String(length=48), nullable=False),
        sa.Column("vendor_name", sa.String(length=180), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("invoice_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("proposal_reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=True),
        sa.Column("decided_by_id", sa.String(length=36), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_count", sa.Integer(), nullable=False),
        sa.Column("last_handoff_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_handoff_by_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payment_proposal_amount_positive"),
        sa.CheckConstraint("version > 0", name="ck_payment_proposal_version_positive"),
        sa.CheckConstraint("handoff_count >= 0", name="ck_payment_proposal_handoff_nonnegative"),
        sa.CheckConstraint("length(currency) = 3", name="ck_payment_proposal_currency_length"),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'cancelled')",
            name="ck_payment_proposal_status",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"],
            name="fk_payment_proposals_invoice_id_invoices", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_decision_id"], ["review_decisions.id"],
            name="fk_payment_proposals_review_decision_id_review_decisions", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"], ["vendors.id"],
            name="fk_payment_proposals_vendor_id_vendors", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_payment_proposals_created_by_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"], ["users.id"],
            name="fk_payment_proposals_decided_by_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["last_handoff_by_id"], ["users.id"],
            name="fk_payment_proposals_last_handoff_by_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_proposals"),
        sa.UniqueConstraint("invoice_id", "version", name="uq_payment_proposal_invoice_version"),
    )
    op.create_index("ix_payment_proposals_invoice_id", TABLE_NAME, ["invoice_id"], unique=False)
    op.create_index("ix_payment_proposals_status_created_at", TABLE_NAME, ["status", "created_at"], unique=False)

    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"), {"name": APPROVER_ROLE},
    ).first()
    if not existing:
        connection.execute(
            sa.text("INSERT INTO roles (id, name, description) VALUES (:id, :name, :description)"),
            {
                "id": str(uuid.uuid4()),
                "name": APPROVER_ROLE,
                "description": "Independently approve or reject non-executable payment proposals",
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    proposals = connection.execute(sa.text(f"SELECT 1 FROM {TABLE_NAME} LIMIT 1")).first()
    assigned = connection.execute(
        sa.text(
            "SELECT 1 FROM user_role_assignments AS ura "
            "JOIN roles AS r ON r.id = ura.role_id "
            "WHERE r.name = :name LIMIT 1"
        ), {"name": APPROVER_ROLE},
    ).first()
    if proposals or assigned:
        raise RuntimeError(
            "Cannot downgrade while payment-proposal records or approver assignments exist; "
            "preserve the data and migrate it explicitly first."
        )

    connection.execute(sa.text("DELETE FROM roles WHERE name = :name"), {"name": APPROVER_ROLE})
    op.drop_index("ix_payment_proposals_status_created_at", table_name=TABLE_NAME)
    op.drop_index("ix_payment_proposals_invoice_id", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
