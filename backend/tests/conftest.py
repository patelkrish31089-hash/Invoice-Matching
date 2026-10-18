from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Role, User
from app.security import hash_password


@pytest.fixture()
def api(tmp_path: Path):
    app = create_app(
        database_url="sqlite:///:memory:",
        storage_dir=tmp_path / "protected-storage",
        initialize_schema=True,
        seed_demo=False,
    )
    with TestClient(app) as client:
        db = app.state.session_factory()
        roles = {name: Role(name=name, description=f"test {name}") for name in ("admin", "reviewer", "ap_clerk", "auditor")}
        admin = User(
            email="admin@test.local", display_name="Test Admin",
            password_hash=hash_password("TestAdminPass123!"), roles=[roles["admin"], roles["ap_clerk"], roles["reviewer"]],
        )
        auditor = User(
            email="auditor@test.local", display_name="Test Auditor",
            password_hash=hash_password("TestAuditPass123!"), roles=[roles["auditor"]],
        )
        reviewer = User(
            email="reviewer@test.local", display_name="Test Reviewer",
            password_hash=hash_password("TestReviewPass123!"), roles=[roles["reviewer"]],
        )
        db.add_all([*roles.values(), admin, auditor, reviewer])
        db.commit()
        db.close()
        admin_login = client.post("/api/v1/auth/login", json={"email": "admin@test.local", "password": "TestAdminPass123!"})
        auditor_login = client.post("/api/v1/auth/login", json={"email": "auditor@test.local", "password": "TestAuditPass123!"})
        reviewer_login = client.post("/api/v1/auth/login", json={"email": "reviewer@test.local", "password": "TestReviewPass123!"})
        assert admin_login.status_code == 200, admin_login.text
        assert auditor_login.status_code == 200, auditor_login.text
        assert reviewer_login.status_code == 200, reviewer_login.text
        yield {
            "app": app,
            "client": client,
            "admin": {"Authorization": f"Bearer {admin_login.json()['access_token']}"},
            "auditor": {"Authorization": f"Bearer {auditor_login.json()['access_token']}"},
            "reviewer": {"Authorization": f"Bearer {reviewer_login.json()['access_token']}"},
        }
