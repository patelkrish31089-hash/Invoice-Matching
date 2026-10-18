"""Persist hashed access-session identifiers for server-side token revocation.

Revision ID: c6d2e0a1f7b4
Revises: 7b4a90c1d6e2
Create Date: 2026-10-16
"""
from __future__ import annotations

import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c6d2e0a1f7b4"
down_revision: Union[str, None] = "7b4a90c1d6e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "auth_sessions"
USER_EXPIRY_INDEX = "ix_auth_sessions_user_expiry"
EXPIRY_INDEX = "ix_auth_sessions_expiry_epoch"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at_epoch", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_sessions_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash", name="pk_auth_sessions"),
    )
    op.create_index(USER_EXPIRY_INDEX, TABLE_NAME, ["user_id", "expires_at_epoch"], unique=False)
    op.create_index(EXPIRY_INDEX, TABLE_NAME, ["expires_at_epoch"], unique=False)


def downgrade() -> None:
    connection = op.get_bind()
    now = int(time.time())
    active_sessions = connection.execute(
        sa.text(f"SELECT 1 FROM {TABLE_NAME} WHERE expires_at_epoch > :now LIMIT 1"),
        {"now": now},
    ).first()
    if active_sessions:
        raise RuntimeError(
            "Cannot downgrade while access sessions are unexpired; wait until all issued tokens expire "
            "or rotate SECRET_KEY and use a separately reviewed recovery procedure."
        )
    op.drop_index(EXPIRY_INDEX, table_name=TABLE_NAME)
    op.drop_index(USER_EXPIRY_INDEX, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
