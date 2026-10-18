"""Add optional, non-sensitive source identities for safe ERP sandbox sync.

Revision ID: d170fe32a6b1
Revises: 65e08a82bd31
Create Date: 2026-10-15
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d170fe32a6b1"
down_revision: Union[str, None] = "65e08a82bd31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


IDENTITY_TABLES = (
    ("vendors", "uq_vendors_external_identity", "ck_vendors_external_identity_pair"),
    ("purchase_orders", "uq_purchase_orders_external_identity", "ck_purchase_orders_external_identity_pair"),
    ("goods_receipts", "uq_goods_receipts_external_identity", "ck_goods_receipts_external_identity_pair"),
)


def upgrade() -> None:
    # Nullable columns preserve all existing local/demo rows. The pair check and unique
    # constraint make repeated external imports idempotent without taking ownership of
    # manually created records or storing integration credentials in the database.
    for table_name, unique_name, check_name in IDENTITY_TABLES:
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.add_column(sa.Column("external_source", sa.String(length=64), nullable=True))
            batch_op.add_column(sa.Column("external_id", sa.String(length=160), nullable=True))
            batch_op.create_unique_constraint(unique_name, ["external_source", "external_id"])
            batch_op.create_check_constraint(
                check_name,
                "(external_source IS NULL AND external_id IS NULL) OR "
                "(external_source IS NOT NULL AND external_id IS NOT NULL)",
            )


def downgrade() -> None:
    for table_name, unique_name, check_name in reversed(IDENTITY_TABLES):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.drop_constraint(check_name, type_="check")
            batch_op.drop_constraint(unique_name, type_="unique")
            batch_op.drop_column("external_id")
            batch_op.drop_column("external_source")
