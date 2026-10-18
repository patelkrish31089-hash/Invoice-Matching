from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "LedgerLens AP Intelligence"
    app_env: str = "development"
    database_url: str = "sqlite:///./invoice_fraud.db"
    secret_key: str = "local-development-only-change-before-deployment"
    access_token_expire_minutes: int = 480
    storage_dir: str = "./storage"
    max_upload_mb: int = 20
    ocr_timeout_seconds: int = 20
    ocr_document_timeout_seconds: int = 300
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    auto_create_schema: bool = True
    demo_seed: bool = True
    task_mode: str = "background"  # background | celery
    redis_url: str = "redis://localhost:6379/0"
    outbox_poll_interval_seconds: int = 5
    layout_model_path: str | None = None
    layout_model_device: str = "cpu"
    layout_model_min_confidence: float = 0.75
    erp_sandbox_base_url: str | None = None
    erp_sandbox_token: str | None = None
    erp_sandbox_timeout_seconds: int = 15
    email_ingest_webhook_secret: str | None = None
    email_ingest_allowed_senders: str = ""
    email_ingest_max_message_mb: int = 25
    email_ingest_signature_tolerance_seconds: int = 300
    duplicate_embedding_model_path: str | None = None
    duplicate_embedding_similarity_threshold: float = 0.92
    malware_scan_mode: str = "off"  # off | clamd; production must require a scanner
    malware_scan_required: bool = False
    clamav_host: str | None = None
    clamav_port: int = 3310
    clamav_timeout_seconds: int = 30
    clamav_max_stream_mb: int = 25
    quarantine_dir: str = "./quarantine"
    upload_recovery_grace_seconds: int = 3600
    upload_recovery_batch_size: int = 100
    tolerance_absolute: str = "0.05"
    tolerance_percent: str = "0.005"
    tolerance_policy: str = "stricter"  # stricter | either
    benford_min_sample: int = 110
    auto_payment_enabled: bool = False

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_allowed_origins.split(",") if item.strip()]

    @property
    def storage_path(self) -> Path:
        path = Path(self.storage_dir)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def quarantine_path(self) -> Path:
        path = Path(self.quarantine_dir)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


settings = Settings()
