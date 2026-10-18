"""Add vendor-bound upload identities without broadening invoice access.

Revision ID: 7b4a90c1d6e2
Revises: 4f0c9d2e6a31
Create Date: 2026-10-15
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b4a90c1d6e2"
down_revision: Union[str, None] = "4f0c9d2e6a31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


FK_NAME = "fk_users_vendor_id_vendors"
INDEX_NAME = "ix_users_vendor_id"


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("vendor_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(FK_NAME, "vendors", ["vendor_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index(INDEX_NAME, ["vendor_id"], unique=False)

    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"), {"name": "vendor"}
    ).first()
    if not existing:
        connection.execute(
            sa.text("INSERT INTO roles (id, name, description) VALUES (:id, :name, :description)"),
            {
                "id": str(uuid.uuid4()),
                "name": "vendor",
                "description": "Upload invoices for the vendor account bound to this user",
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    assigned = connection.execute(
        sa.text(
            "SELECT 1 FROM user_role_assignments AS ura "
            "JOIN roles AS r ON r.id = ura.role_id WHERE r.name = :name LIMIT 1"
        ), {"name": "vendor"}
    ).first()
    bound_users = connection.execute(
        sa.text("SELECT 1 FROM users WHERE vendor_id IS NOT NULL LIMIT 1")
    ).first()
    if assigned or bound_users:
        raise RuntimeError(
            "Cannot downgrade vendor upload scope while vendor-role assignments or vendor-bound users exist; preserve those accounts and migrate them explicitly first."
        )

    connection.execute(sa.text("DELETE FROM roles WHERE name = :name"), {"name": "vendor"})
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(INDEX_NAME)
        batch_op.drop_constraint(FK_NAME, type_="foreignkey")
        batch_op.drop_column("vendor_id")
