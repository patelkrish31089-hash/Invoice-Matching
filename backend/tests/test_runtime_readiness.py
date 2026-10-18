from __future__ import annotations

import pytest

from app.config import settings
from app.main import validate_runtime_configuration


def _configure_safe_production(monkeypatch):
    values = {
        "app_env": "production",
        "secret_key": "unit-test-production-secret-with-at-least-32-characters",
        "database_url": "postgresql+psycopg://app_user:long-test-password@postgres.internal:5432/invoiceguard",
        "task_mode": "celery",
        "redis_url": "rediss://:long-test-password@redis.internal:6379/0",
        "storage_dir": "/srv/invoiceguard/documents",
        "quarantine_dir": "/srv/invoiceguard/quarantine",
        "cors_allowed_origins": "https://ap.example.test",
        "malware_scan_mode": "clamd",
        "malware_scan_required": True,
        "clamav_host": "clamav.internal",
        "clamav_max_stream_mb": 25,
        "max_upload_mb": 20,
        "auto_create_schema": False,
        "demo_seed": False,
        "email_ingest_webhook_secret": None,
        "email_ingest_allowed_senders": "",
        "erp_sandbox_base_url": None,
        "erp_sandbox_token": None,
    }
    for field, value in values.items():
        monkeypatch.setattr(settings, field, value)


def test_production_configuration_accepts_explicit_secure_dependencies(monkeypatch):
    _configure_safe_production(monkeypatch)
    validate_runtime_configuration(initialize_schema=False, seed_demo=False)


def test_production_configuration_requires_scanner_and_fitting_stream_limit(monkeypatch):
    _configure_safe_production(monkeypatch)
    monkeypatch.setattr(settings, "malware_scan_required", False)
    with pytest.raises(RuntimeError, match="Production requires MALWARE_SCAN_REQUIRED"):
        validate_runtime_configuration(initialize_schema=False, seed_demo=False)

    _configure_safe_production(monkeypatch)
    monkeypatch.setattr(settings, "max_upload_mb", 30)
    with pytest.raises(RuntimeError, match="MAX_UPLOAD_MB cannot exceed CLAMAV_MAX_STREAM_MB"):
        validate_runtime_configuration(initialize_schema=False, seed_demo=False)


def test_production_configuration_rejects_unauthenticated_plain_redis(monkeypatch):
    _configure_safe_production(monkeypatch)
    monkeypatch.setattr(settings, "redis_url", "redis://redis.internal:6379/0")
    with pytest.raises(RuntimeError, match="non-placeholder Redis password"):
        validate_runtime_configuration(initialize_schema=False, seed_demo=False)


def test_health_live_ready_and_worker_endpoints_are_distinct(api):
    client = api["client"]
    live = client.get("/api/v1/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "alive"}

    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"
    assert ready.json()["checks"]["database"] == "ok"
    assert ready.json()["checks"]["storage"] == "ok"

    worker = client.get("/api/v1/health/worker")
    assert worker.status_code == 200
    assert worker.json()["status"] == "not_required"
