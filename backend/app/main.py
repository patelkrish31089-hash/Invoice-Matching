from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import logging
import os
import re
import uuid
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.api import auth, email_ingestion, invoices, operations, payment_proposals, review, user_admin
from app.config import settings
from app.db import Base, create_database_engine, create_session_factory, get_db
from app.models import User
from app.security import require_roles
from app.upload_recovery import recover_stale_upload_receipts
from app.services.document_processing import tesseract_available

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ledgerlens")


def validate_runtime_configuration(*, initialize_schema: bool, seed_demo: bool) -> None:
    """Fail startup for unsafe production configuration before serving requests."""
    mode = settings.task_mode.strip().lower()
    if mode not in {"background", "celery"}:
        raise RuntimeError("TASK_MODE must be either 'background' or 'celery'")
    if not 1 <= int(settings.max_upload_mb) <= 100:
        raise RuntimeError("MAX_UPLOAD_MB must be between 1 and 100")
    scan_mode = settings.malware_scan_mode.strip().lower()
    if scan_mode not in {"off", "clamd"}:
        raise RuntimeError("MALWARE_SCAN_MODE must be either 'off' or 'clamd'")
    if not 1 <= int(settings.clamav_timeout_seconds) <= 120:
        raise RuntimeError("CLAMAV_TIMEOUT_SECONDS must be between 1 and 120")
    if not 60 <= int(settings.upload_recovery_grace_seconds) <= 86_400:
        raise RuntimeError("UPLOAD_RECOVERY_GRACE_SECONDS must be between 60 and 86400")
    if not 1 <= int(settings.upload_recovery_batch_size) <= 1000:
        raise RuntimeError("UPLOAD_RECOVERY_BATCH_SIZE must be between 1 and 1000")
    if not 1 <= int(settings.clamav_max_stream_mb) <= 100:
        raise RuntimeError("CLAMAV_MAX_STREAM_MB must be between 1 and 100")
    if settings.malware_scan_required and (scan_mode != "clamd" or not settings.clamav_host):
        raise RuntimeError("Required malware scanning needs MALWARE_SCAN_MODE=clamd and CLAMAV_HOST")
    if settings.malware_scan_required and int(settings.max_upload_mb) > int(settings.clamav_max_stream_mb):
        raise RuntimeError("MAX_UPLOAD_MB cannot exceed CLAMAV_MAX_STREAM_MB while malware scanning is required")
    if settings.email_ingest_webhook_secret and len(settings.email_ingest_webhook_secret.strip()) < 32:
        raise RuntimeError("EMAIL_INGEST_WEBHOOK_SECRET must contain at least 32 characters when configured")
    if settings.email_ingest_webhook_secret and not settings.email_ingest_allowed_senders.strip():
        raise RuntimeError("EMAIL_INGEST_ALLOWED_SENDERS is required when the email webhook secret is configured")
    if bool(settings.erp_sandbox_base_url) != bool(settings.erp_sandbox_token):
        raise RuntimeError("ERP_SANDBOX_BASE_URL and ERP_SANDBOX_TOKEN must be configured together")

    if settings.app_env.strip().lower() != "production":
        return
    secret_value = settings.secret_key.strip()
    if (
        secret_value == "local-development-only-change-before-deployment"
        or len(secret_value) < 32
        or secret_value.casefold().startswith(("replace-", "change-me", "changeme", "your-"))
    ):
        raise RuntimeError("Production SECRET_KEY must be a unique random value of at least 32 characters, not a placeholder")
    if initialize_schema:
        raise RuntimeError("AUTO_CREATE_SCHEMA must be disabled in production; use Alembic migrations")
    if seed_demo:
        raise RuntimeError("DEMO_SEED must be disabled in production")
    database = urlparse(settings.database_url)
    if database.scheme != "postgresql+psycopg" or not database.hostname or not database.username or not database.password:
        raise RuntimeError("Production DATABASE_URL must use PostgreSQL/psycopg with an explicit remote service and credentials")
    if database.hostname in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Production DATABASE_URL must not point to loopback")
    if len(database.password or "") < 16 or (database.password or "").casefold().startswith(("replace-", "change-me", "changeme")):
        raise RuntimeError("Production DATABASE_URL must use a non-placeholder database password of at least 16 characters")
    if mode != "celery":
        raise RuntimeError("Production TASK_MODE must be 'celery'; use a separate worker and outbox dispatcher")
    redis = urlparse(settings.redis_url)
    if redis.scheme not in {"redis", "rediss"} or not redis.hostname or redis.hostname in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Production REDIS_URL must point to a reachable secured Redis service")
    if not redis.password or len(redis.password) < 16 or redis.password.casefold().startswith(("replace-", "change-me", "changeme")):
        raise RuntimeError("Production REDIS_URL must use TLS or require a non-placeholder Redis password of at least 16 characters")
    if not Path(settings.storage_dir).is_absolute() or not Path(settings.quarantine_dir).is_absolute():
        raise RuntimeError("Production STORAGE_DIR and QUARANTINE_DIR must be absolute persistent paths")
    if "*" in settings.cors_origins:
        raise RuntimeError("Production CORS_ALLOWED_ORIGINS must list explicit trusted origins")
    if not settings.malware_scan_required or scan_mode != "clamd" or not settings.clamav_host:
        raise RuntimeError("Production requires MALWARE_SCAN_REQUIRED=true and a configured ClamAV service")


def _ensure_local_additive_schema(engine) -> None:
    """Add local-only compatibility fields and preserve queued jobs in the dispatch outbox."""
    inspector = inspect(engine)
    if not inspector.has_table("processing_jobs"):
        return
    columns = {column["name"] for column in inspector.get_columns("processing_jobs")}
    if "heartbeat_at" not in columns:
        try:
            with engine.begin() as connection:
                if engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("ALTER TABLE processing_jobs ADD COLUMN heartbeat_at DATETIME")
                elif engine.dialect.name == "postgresql":
                    connection.exec_driver_sql("ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITH TIME ZONE")
                else:
                    raise RuntimeError("Apply the Alembic migration before starting with this database dialect")
        except Exception as exc:
            # Another local app process may have applied the same additive change at startup.
            refreshed = inspect(engine)
            if "heartbeat_at" not in {column["name"] for column in refreshed.get_columns("processing_jobs")}:
                raise RuntimeError("Local database needs the processing-job heartbeat migration; apply Alembic before startup") from exc

    # `create_all` does not alter tables in an existing local SQLite demo database.
    # Add only nullable external identity fields for old local schemas; production
    # deployments use the Alembic migration, which also enforces pair constraints.
    identity_tables = (
        ("vendors", "ux_local_vendors_external_identity"),
        ("purchase_orders", "ux_local_purchase_orders_external_identity"),
        ("goods_receipts", "ux_local_goods_receipts_external_identity"),
    )
    for table_name, index_name in identity_tables:
        if not inspect(engine).has_table(table_name):
            continue
        columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
        missing = [
            ("external_source", "VARCHAR(64)"), ("external_id", "VARCHAR(160)"),
        ]
        missing = [(name, sql_type) for name, sql_type in missing if name not in columns]
        if not missing:
            continue
        try:
            with engine.begin() as connection:
                for column_name, sql_type in missing:
                    if engine.dialect.name == "sqlite":
                        connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type}")
                    elif engine.dialect.name == "postgresql":
                        connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {sql_type}")
                    else:
                        raise RuntimeError("Apply Alembic migrations before starting with this database dialect")
                connection.exec_driver_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name} (external_source, external_id)")
        except Exception as exc:
            refreshed_inspector = inspect(engine)
            refreshed_columns = {column["name"] for column in refreshed_inspector.get_columns(table_name)}
            refreshed_indexes = {index["name"] for index in refreshed_inspector.get_indexes(table_name)}
            if not {"external_source", "external_id"}.issubset(refreshed_columns) or index_name not in refreshed_indexes:
                raise RuntimeError(f"Local database needs the ERP identity migration for {table_name}; apply Alembic before startup") from exc

    # A local `create_all` call cannot add this nullable scope binding to an older
    # users table, so preserve existing accounts and add it in place. Deployed
    # environments use the additive Alembic migration instead.
    if inspect(engine).has_table("users") and inspect(engine).has_table("vendors"):
        user_columns = {column["name"] for column in inspect(engine).get_columns("users")}
        try:
            with engine.begin() as connection:
                if "vendor_id" not in user_columns:
                    if engine.dialect.name == "sqlite":
                        connection.exec_driver_sql(
                            "ALTER TABLE users ADD COLUMN vendor_id VARCHAR(36) "
                            "REFERENCES vendors(id) ON DELETE SET NULL"
                        )
                    elif engine.dialect.name == "postgresql":
                        connection.exec_driver_sql(
                            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vendor_id VARCHAR(36) "
                            "REFERENCES vendors(id) ON DELETE SET NULL"
                        )
                    else:
                        raise RuntimeError("Apply the vendor upload-scope Alembic migration before startup")
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_users_vendor_id ON users (vendor_id)")
                if engine.dialect.name == "sqlite":
                    connection.execute(
                        text(
                            "INSERT OR IGNORE INTO roles (id, name, description) "
                            "VALUES (:id, 'vendor', :description)"
                        ),
                        {"id": str(uuid.uuid4()), "description": "Upload invoices for the vendor account bound to this user"},
                    )
                    connection.execute(
                        text(
                            "INSERT OR IGNORE INTO roles (id, name, description) "
                            "VALUES (:id, 'approver', :description)"
                        ),
                        {"id": str(uuid.uuid4()), "description": "Independently approve or reject non-executable payment proposals"},
                    )
                elif engine.dialect.name == "postgresql":
                    connection.execute(
                        text(
                            "INSERT INTO roles (id, name, description) VALUES (:id, 'vendor', :description) "
                            "ON CONFLICT (name) DO NOTHING"
                        ),
                        {"id": str(uuid.uuid4()), "description": "Upload invoices for the vendor account bound to this user"},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO roles (id, name, description) VALUES (:id, 'approver', :description) "
                            "ON CONFLICT (name) DO NOTHING"
                        ),
                        {"id": str(uuid.uuid4()), "description": "Independently approve or reject non-executable payment proposals"},
                    )
                else:
                    raise RuntimeError("Apply the vendor upload-scope Alembic migration before startup")
        except Exception as exc:
            refreshed = inspect(engine)
            refreshed_columns = {column["name"] for column in refreshed.get_columns("users")}
            refreshed_indexes = {index["name"] for index in refreshed.get_indexes("users")}
            with engine.connect() as connection:
                vendor_role_exists = connection.execute(
                    text("SELECT 1 FROM roles WHERE name = :name"), {"name": "vendor"},
                ).first() is not None
            if "vendor_id" not in refreshed_columns or "ix_users_vendor_id" not in refreshed_indexes or not vendor_role_exists:
                raise RuntimeError("Local database needs the vendor upload-scope migration; apply Alembic before startup") from exc

    if not inspect(engine).has_table("processing_outbox"):
        return
    from datetime import datetime, timezone
    from uuid import uuid4

    insert_sql = (
        "INSERT OR IGNORE INTO processing_outbox "
        "(id, job_id, invoice_id, actor_id, event_type, state, attempts, available_at, created_at) "
        "VALUES (:id, :job_id, :invoice_id, NULL, 'process_invoice', 'pending', 0, :now, :now)"
        if engine.dialect.name == "sqlite" else
        "INSERT INTO processing_outbox "
        "(id, job_id, invoice_id, actor_id, event_type, state, attempts, available_at, created_at) "
        "VALUES (:id, :job_id, :invoice_id, NULL, 'process_invoice', 'pending', 0, :now, :now) "
        "ON CONFLICT (job_id, event_type) DO NOTHING"
    )
    try:
        with engine.begin() as connection:
            jobs = connection.execute(text(
                "SELECT j.id, j.invoice_id FROM processing_jobs AS j "
                "WHERE j.state = 'queued' AND NOT EXISTS ("
                "SELECT 1 FROM processing_outbox AS o WHERE o.job_id = j.id AND o.event_type = 'process_invoice')"
            )).mappings().all()
            now = datetime.now(timezone.utc)
            for job in jobs:
                connection.execute(text(insert_sql), {
                    "id": str(uuid4()), "job_id": job["id"], "invoice_id": job["invoice_id"], "now": now,
                })
    except Exception as exc:
        raise RuntimeError("Local database outbox compatibility/backfill failed; inspect migrations before startup") from exc


async def _outbox_pump(session_factory, storage_dir: Path) -> None:
    """Recover persisted dispatch intents after API restart in local/single-host deployments."""
    from app.outbox import dispatch_pending_outbox

    interval = max(1, int(settings.outbox_poll_interval_seconds))
    await asyncio.sleep(interval)
    while True:
        try:
            await asyncio.to_thread(dispatch_pending_outbox, session_factory, storage_dir, limit=25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("outbox_pump_failed error_type=%s", type(exc).__name__)
        await asyncio.sleep(interval)


def create_app(
    *,
    database_url: str | None = None,
    storage_dir: str | Path | None = None,
    initialize_schema: bool | None = None,
    seed_demo: bool | None = None,
) -> FastAPI:
    db_url = database_url or settings.database_url
    upload_root = Path(storage_dir).resolve() if storage_dir else settings.storage_path.resolve()
    quarantine_root = settings.quarantine_path.resolve() if storage_dir is None else Path(storage_dir).resolve().parent / "quarantine"
    should_initialize = settings.auto_create_schema if initialize_schema is None else initialize_schema
    should_seed = settings.demo_seed if seed_demo is None else seed_demo
    engine = create_database_engine(db_url)
    session_factory = create_session_factory(engine, immediate=True)
    read_session_factory = create_session_factory(engine, immediate=False)
    upload_root.mkdir(parents=True, exist_ok=True)
    quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        quarantine_root.chmod(0o700)
    except OSError:
        # Storage ACLs are deployment-specific; readiness and scanner failures remain explicit.
        pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_runtime_configuration(initialize_schema=should_initialize, seed_demo=should_seed)
        if should_initialize:
            Base.metadata.create_all(engine)
            _ensure_local_additive_schema(engine)
        if should_seed:
            from app.seed import seed_demo_data
            seeded_jobs = seed_demo_data(session_factory, upload_root)
            if seeded_jobs:
                logger.info("Seeded synthetic demonstration data; processed %d representative invoice documents", len(seeded_jobs))
        recovery_db = session_factory()
        try:
            recovery_result = recover_stale_upload_receipts(
                recovery_db,
                upload_root,
                older_than_seconds=settings.upload_recovery_grace_seconds,
                limit=settings.upload_recovery_batch_size,
            )
            if recovery_result["examined"]:
                logger.warning("stale_upload_receipts_recovered %s", recovery_result)
        finally:
            recovery_db.close()
        pump_task = asyncio.create_task(_outbox_pump(session_factory, upload_root))
        try:
            yield
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            engine.dispose()

    app = FastAPI(
        title="LedgerLens · Invoice Matching & Fraud Review",
        description=(
            "Local-first AP intelligence demo: document extraction, explainable three-way matching, "
            "heuristic fraud screening, and human review. Risk scores are not fraud probabilities."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.read_session_factory = read_session_factory
    app.state.storage_dir = upload_root
    app.state.quarantine_dir = quarantine_root
    app.state.app_env = settings.app_env

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["Content-Disposition", "X-Request-ID"],
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        candidate = request.headers.get("X-Request-ID", "")
        request_id = candidate if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}", candidate) else uuid.uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("http_request_failed request_id=%s method=%s", request_id, request.method)
            raise
        response.headers.setdefault("X-Request-ID", request_id)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api/") else "no-cache")
        if response.status_code >= 500:
            logger.error("http_request_failed request_id=%s method=%s status=%s", request_id, request.method, response.status_code)
        return response

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(email_ingestion.router, prefix="/api/v1")
    app.include_router(invoices.router, prefix="/api/v1")
    app.include_router(review.router, prefix="/api/v1")
    app.include_router(payment_proposals.router, prefix="/api/v1")
    app.include_router(operations.router, prefix="/api/v1")
    app.include_router(user_admin.router, prefix="/api/v1")

    @app.get("/api/v1/health/live", tags=["system"])
    def health_live():
        return {"status": "alive"}

    @app.get("/api/v1/health", tags=["system"])
    def health(db: Session = Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
            database = "connected"
        except Exception as exc:
            logger.error("database_health_check_failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
        ocr_ok = tesseract_available()
        return {
            "status": "ok" if ocr_ok else "degraded",
            "database": database,
            "storage": "available" if app.state.storage_dir.is_dir() and os.access(app.state.storage_dir, os.W_OK | os.X_OK) else "unavailable",
            "task_mode": settings.task_mode.lower(),
            "ocr_tesseract_available": ocr_ok,
            "digital_pdf_text_extraction": "available",
            "scanned_document_ocr": "available" if ocr_ok else "requires local Tesseract installation",
            "malware_scan": "required" if settings.malware_scan_required else ("enabled_optional" if settings.malware_scan_mode.lower() == "clamd" else "disabled_demo_only"),
            "auto_payment_enabled": False,
            "environment": settings.app_env,
        }

    @app.get("/api/v1/health/ready", tags=["system"])
    def health_ready(db: Session = Depends(get_db)):
        checks: dict[str, str] = {"database": "ok", "storage": "ok", "quarantine": "ok", "broker": "not_required", "malware_scanner": "not_required", "ocr": "available" if tesseract_available() else "unavailable"}
        try:
            db.execute(text("SELECT 1"))
        except Exception as exc:
            checks["database"] = "unavailable"
            logger.error("database_readiness_check_failed error_type=%s", type(exc).__name__)
        storage_path = app.state.storage_dir
        if not storage_path.is_dir() or not os.access(storage_path, os.W_OK | os.X_OK):
            checks["storage"] = "unavailable"
        quarantine_path = app.state.quarantine_dir
        if not quarantine_path.is_dir() or not os.access(quarantine_path, os.W_OK | os.X_OK):
            checks["quarantine"] = "unavailable"
        if settings.task_mode.lower() == "celery":
            try:
                from redis import Redis
                redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
                try:
                    redis_client.ping()
                finally:
                    redis_client.close()
                checks["broker"] = "ok"
            except Exception as exc:
                checks["broker"] = "unavailable"
                logger.error("broker_readiness_check_failed error_type=%s", type(exc).__name__)
        if settings.malware_scan_required:
            try:
                from app.services.malware_scan import clamd_ping
                clamd_ping(settings.clamav_host or "", int(settings.clamav_port), float(settings.clamav_timeout_seconds))
                checks["malware_scanner"] = "ok"
            except Exception as exc:
                checks["malware_scanner"] = "unavailable"
                logger.error("malware_scanner_readiness_check_failed error_type=%s", type(exc).__name__)
        critical = ["database", "storage"]
        if settings.malware_scan_required:
            critical.append("quarantine")
        if settings.task_mode.lower() == "celery":
            critical.append("broker")
        if settings.malware_scan_required:
            critical.append("malware_scanner")
        if settings.app_env.lower() == "production":
            critical.append("ocr")
        ready = all(checks[name] == "ok" or (name == "ocr" and checks[name] == "available") for name in critical)
        payload = {"status": "ready" if ready else "not_ready", "checks": checks, "worker": "checked_separately"}
        if not ready:
            raise HTTPException(status_code=503, detail=payload)
        return payload

    @app.get("/api/v1/health/worker", tags=["system"])
    def health_worker():
        if settings.task_mode.lower() != "celery":
            return {"status": "not_required", "task_mode": settings.task_mode.lower()}
        try:
            from app.tasks import celery_app
            responses = celery_app.control.inspect(timeout=1.0).ping() or {}
        except Exception as exc:
            logger.error("worker_health_check_failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Worker status unavailable") from exc
        if not responses:
            raise HTTPException(status_code=503, detail="No Celery worker is responding")
        return {"status": "healthy", "workers": len(responses)}

    @app.get("/api/v1/auth/permissions", tags=["authentication"])
    def permissions(user: User = Depends(require_roles("admin", "ap_clerk", "reviewer", "auditor"))):
        roles = sorted(role.name for role in user.roles)
        return {"roles": roles, "capabilities": {
            "upload_invoices": bool(set(roles) & {"admin", "ap_clerk", "reviewer"}),
            "correct_extractions": bool(set(roles) & {"admin", "ap_clerk", "reviewer"}),
            "make_review_decisions": bool(set(roles) & {"admin", "reviewer"}),
            "manage_reference_data": bool(set(roles) & {"admin", "ap_clerk"}),
            "view_audit_logs": bool(set(roles) & {"admin", "auditor"}),
        }}

    @app.get("/", include_in_schema=False)
    def root():
        return {"application": "LedgerLens AP Intelligence", "api": "/api/v1", "documentation": "/docs", "ui": "Run the Vite frontend on port 5173."}

    return app


app = create_app()
