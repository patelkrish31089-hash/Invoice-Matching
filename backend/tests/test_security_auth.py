from __future__ import annotations

import time

from app.models import AuthSession, User
from app.security import create_access_token, decode_access_token, token_fingerprint


def test_access_tokens_are_unique_for_same_user_and_second(monkeypatch):
    class FixedClock:
        @staticmethod
        def time():
            return 1_800_000_000

    monkeypatch.setattr("app.security.time", FixedClock)
    first = create_access_token("synthetic-user", secret="test-secret")
    second = create_access_token("synthetic-user", secret="test-secret")
    first_claims = decode_access_token(first, secret="test-secret")
    second_claims = decode_access_token(second, secret="test-secret")

    assert first != second
    assert first_claims["sub"] == second_claims["sub"] == "synthetic-user"
    assert first_claims["iat"] == second_claims["iat"] == 1_800_000_000
    assert first_claims["jti"] != second_claims["jti"]


def test_login_accepts_multiple_registered_email_accounts(api):
    client = api["client"]
    with api["app"].state.session_factory() as db:
        admin = db.query(User).filter_by(email="admin@test.local").one()
        admin.email = "Admin@Test.Local"
        db.commit()

    admin_login = client.post("/api/v1/auth/login", json={
        "email": "  admin@test.local  ", "password": "TestAdminPass123!",
    })
    reviewer_login = client.post("/api/v1/auth/login", json={
        "email": "reviewer@test.local", "password": "TestReviewPass123!",
    })

    assert admin_login.status_code == reviewer_login.status_code == 200
    admin_token = admin_login.json()["access_token"]
    reviewer_token = reviewer_login.json()["access_token"]
    assert admin_token != reviewer_token
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()["email"] == "Admin@Test.Local"
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {reviewer_token}"}).json()["email"] == "reviewer@test.local"


def test_login_persists_only_a_fingerprint_and_logout_revokes_one_session(api):
    client = api["client"]
    first_login = client.post("/api/v1/auth/login", json={
        "email": "admin@test.local", "password": "TestAdminPass123!",
    })
    second_login = client.post("/api/v1/auth/login", json={
        "email": "admin@test.local", "password": "TestAdminPass123!",
    })
    assert first_login.status_code == second_login.status_code == 200
    first_token = first_login.json()["access_token"]
    second_token = second_login.json()["access_token"]
    first_claims = decode_access_token(first_token)
    second_claims = decode_access_token(second_token)
    first_hash = token_fingerprint(first_claims["jti"])
    second_hash = token_fingerprint(second_claims["jti"])
    assert first_hash != second_hash

    with api["app"].state.session_factory() as db:
        first_session = db.get(AuthSession, first_hash)
        second_session = db.get(AuthSession, second_hash)
        assert first_session is not None and second_session is not None
        assert first_session.user_id == second_session.user_id
        assert first_session.expires_at_epoch == first_claims["exp"]
        assert first_claims["jti"] not in first_session.token_hash
        assert first_token not in first_session.token_hash
        assert first_session.revoked_at is None
        assert second_session.revoked_at is None

    logout = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {first_token}"})
    assert logout.status_code == 200
    assert logout.json()["ok"] is True
    revoked = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {first_token}"})
    still_active = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {second_token}"})
    assert revoked.status_code == 401
    assert revoked.json()["detail"] == "Invalid bearer token"
    assert still_active.status_code == 200

    with api["app"].state.session_factory() as db:
        assert db.get(AuthSession, first_hash).revoked_at is not None
        assert db.get(AuthSession, second_hash).revoked_at is None


def test_refresh_rotates_and_revokes_the_previous_access_session(api):
    client = api["client"]
    login = client.post("/api/v1/auth/login", json={
        "email": "reviewer@test.local", "password": "TestReviewPass123!",
    })
    assert login.status_code == 200
    old_token = login.json()["access_token"]

    refreshed = client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {old_token}"})
    assert refreshed.status_code == 200
    new_token = refreshed.json()["access_token"]
    assert new_token != old_token
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200

    replay = client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {old_token}"})
    assert replay.status_code == 401


def test_login_prunes_only_expired_session_metadata(api):
    client = api["client"]
    first = client.post("/api/v1/auth/login", json={
        "email": "reviewer@test.local", "password": "TestReviewPass123!",
    })
    second = client.post("/api/v1/auth/login", json={
        "email": "reviewer@test.local", "password": "TestReviewPass123!",
    })
    assert first.status_code == second.status_code == 200
    first_jti = decode_access_token(first.json()["access_token"])["jti"]
    second_jti = decode_access_token(second.json()["access_token"])["jti"]
    expired_hash = token_fingerprint(first_jti)
    active_hash = token_fingerprint(second_jti)

    with api["app"].state.session_factory() as db:
        expired_session = db.get(AuthSession, expired_hash)
        active_session = db.get(AuthSession, active_hash)
        assert expired_session is not None and active_session is not None
        expired_session.expires_at_epoch = int(time.time()) - 1
        db.commit()

    third = client.post("/api/v1/auth/login", json={
        "email": "reviewer@test.local", "password": "TestReviewPass123!",
    })
    assert third.status_code == 200
    third_hash = token_fingerprint(decode_access_token(third.json()["access_token"])["jti"])
    with api["app"].state.session_factory() as db:
        assert db.get(AuthSession, expired_hash) is None
        assert db.get(AuthSession, active_hash) is not None
        assert db.get(AuthSession, third_hash) is not None


def test_auth_me_distinguishes_missing_invalid_expired_and_inactive_credentials(api, caplog):
    caplog.set_level("WARNING", logger="ledgerlens.auth")
    client = api["client"]

    missing = client.get("/api/v1/auth/me", headers={"X-Request-ID": "auth-missing-001"})
    assert missing.status_code == 401
    assert missing.json()["detail"] == "Bearer token required"
    assert missing.headers["X-Request-ID"] == "auth-missing-001"

    invalid = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-valid-token"})
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "Invalid bearer token"

    with api["app"].state.session_factory() as db:
        admin = db.query(User).filter(User.email == "admin@test.local").one()
        admin_id = admin.id
    unregistered_token = create_access_token(admin_id)
    unregistered = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {unregistered_token}"})
    assert unregistered.status_code == 401
    assert unregistered.json()["detail"] == "Invalid bearer token"

    expired_token = create_access_token(admin_id, expires_minutes=-1)
    expired = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert expired.status_code == 401
    assert expired.json()["detail"] == "Bearer token expired"

    with api["app"].state.session_factory() as db:
        admin = db.query(User).filter(User.email == "admin@test.local").one()
        admin.is_active = False
        db.commit()
    inactive = client.get("/api/v1/auth/me", headers=api["admin"])
    assert inactive.status_code == 401
    assert inactive.json()["detail"] == "User account is inactive"

    auth_logs = "\n".join(record.getMessage() for record in caplog.records if record.name == "ledgerlens.auth")
    assert "request_id=auth-missing-001 path=/api/v1/auth/me reason=missing_authorization" in auth_logs
    assert "reason=invalid_token" in auth_logs
    assert "reason=inactive_session" in auth_logs
    assert "reason=expired_token" in auth_logs
    assert "reason=inactive_user" in auth_logs
    assert "not-a-valid-token" not in auth_logs


def test_request_id_is_echoed_or_safely_replaced(api):
    client = api["client"]
    accepted = client.get("/api/v1/health", headers={"X-Request-ID": "ui-request-123"})
    assert accepted.status_code == 200
    assert accepted.headers["X-Request-ID"] == "ui-request-123"

    rejected = client.get("/api/v1/health", headers={"X-Request-ID": "bad id"})
    assert rejected.status_code == 200
    generated = rejected.headers["X-Request-ID"]
    assert len(generated) == 32
    assert all(character in "0123456789abcdef" for character in generated)
