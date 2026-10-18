import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import create_database_engine
from app.main import _ensure_local_additive_schema, create_app, validate_runtime_configuration


def test_production_startup_refuses_demo_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "secret_key", "this-is-a-test-secret-that-is-not-the-default")
    app = create_app(database_url="sqlite:///:memory:", storage_dir=tmp_path / "storage", initialize_schema=False, seed_demo=True)
    with pytest.raises(RuntimeError, match="DEMO_SEED must be disabled"):
        with TestClient(app):
            pass


def test_local_schema_compatibility_adds_heartbeat_without_losing_existing_jobs(tmp_path):
    database = tmp_path / "legacy-local.sqlite"
    engine = create_database_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE processing_jobs (
                id VARCHAR(36) PRIMARY KEY, invoice_id VARCHAR(36) NOT NULL,
                state VARCHAR(24) NOT NULL, stage VARCHAR(60) NOT NULL,
                attempts INTEGER NOT NULL, error_message TEXT, queued_at DATETIME NOT NULL,
                started_at DATETIME, finished_at DATETIME
            )
        """)
        connection.exec_driver_sql("INSERT INTO processing_jobs (id, invoice_id, state, stage, attempts, queued_at) VALUES ('job-keep', 'invoice-keep', 'queued', 'queued', 0, '2026-10-12 00:00:00')")
    _ensure_local_additive_schema(engine)
    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(processing_jobs)")}
        existing = connection.exec_driver_sql("SELECT id, state FROM processing_jobs").one()
    assert "heartbeat_at" in columns
    assert existing == ("job-keep", "queued")
    engine.dispose()
