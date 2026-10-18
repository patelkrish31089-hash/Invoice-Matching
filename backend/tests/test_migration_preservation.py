from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text


BASE_REVISION = "4f0c9d2e6a31"
HEAD_REVISION = "a71f3c2e9b40"


def _upgrade(backend_root: Path, database_url: str, revision: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_vendor_binding_migration_preserves_existing_accounts_and_assignments(tmp_path, monkeypatch):
    backend_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'preserved-data.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    # Start from the immediately preceding schema revision, then seed representative
    # pre-existing account, role assignment, and vendor rows before applying the change.
    _upgrade(backend_root, database_url, BASE_REVISION)
    engine = create_engine(database_url)
    original_user = "user-existing-before-vendor-migration"
    original_role = "role-admin-before-vendor-migration"
    original_vendor = "vendor-existing-before-migration"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO roles (id, name, description) VALUES (:id, :name, :description)"
        ), {
            "id": original_role,
            "name": "admin",
            "description": "Existing administrator role",
        })
        connection.execute(text(
            "INSERT INTO users (id, email, display_name, password_hash, is_active, created_at, updated_at) "
            "VALUES (:id, :email, :display_name, :password_hash, :is_active, :created_at, :updated_at)"
        ), {
            "id": original_user,
            "email": "preserved.admin@example.test",
            "display_name": "Preserved Admin",
            "password_hash": "synthetic-preexisting-password-hash",
            "is_active": 1,
            "created_at": "2026-01-02 03:04:05",
            "updated_at": "2026-02-03 04:05:06",
        })
        connection.execute(text(
            "INSERT INTO user_role_assignments (user_id, role_id) VALUES (:user_id, :role_id)"
        ), {"user_id": original_user, "role_id": original_role})
        connection.execute(text(
            "INSERT INTO vendors (id, code, name, aliases, is_active, risk_tier, created_at, updated_at) "
            "VALUES (:id, :code, :name, :aliases, :is_active, :risk_tier, :created_at, :updated_at)"
        ), {
            "id": original_vendor,
            "code": "PRESERVE-001",
            "name": "Synthetic Pre-existing Vendor",
            "aliases": "[]",
            "is_active": 1,
            "risk_tier": "normal",
            "created_at": "2026-01-02 03:04:05",
            "updated_at": "2026-02-03 04:05:06",
        })

    _upgrade(backend_root, database_url, "head")
    try:
        with engine.connect() as connection:
            user = connection.execute(text(
                "SELECT id, email, display_name, password_hash, is_active, vendor_id "
                "FROM users WHERE id = :id"
            ), {"id": original_user}).mappings().one()
            assignments = connection.execute(text(
                "SELECT role_id FROM user_role_assignments WHERE user_id = :id"
            ), {"id": original_user}).scalars().all()
            vendor = connection.execute(text(
                "SELECT code, name, is_active FROM vendors WHERE id = :id"
            ), {"id": original_vendor}).mappings().one()
            vendor_roles = connection.execute(text(
                "SELECT id, description FROM roles WHERE name = 'vendor'"
            )).mappings().all()
            auth_session_table = connection.execute(text(
                "SELECT COUNT(*) FROM auth_sessions"
            )).scalar_one()
            proposal_table = connection.execute(text(
                "SELECT COUNT(*) FROM payment_proposals"
            )).scalar_one()
            approver_roles = connection.execute(text(
                "SELECT id, description FROM roles WHERE name = 'approver'"
            )).mappings().all()
            current_revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

        assert current_revision == HEAD_REVISION
        assert user["id"] == original_user
        assert user["email"] == "preserved.admin@example.test"
        assert user["display_name"] == "Preserved Admin"
        assert user["password_hash"] == "synthetic-preexisting-password-hash"
        assert user["is_active"] == 1
        assert user["vendor_id"] is None
        assert assignments == [original_role]
        assert vendor["code"] == "PRESERVE-001"
        assert vendor["name"] == "Synthetic Pre-existing Vendor"
        assert vendor["is_active"] == 1
        assert len(vendor_roles) == 1
        assert vendor_roles[0]["description"] == "Upload invoices for the vendor account bound to this user"
        assert auth_session_table == 0
        assert proposal_table == 0
        assert len(approver_roles) == 1
        assert approver_roles[0]["description"] == "Independently approve or reject non-executable payment proposals"
    finally:
        engine.dispose()
