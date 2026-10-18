from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select

from app.api.invoices import _upload_receipt_key_hash
from app.config import settings
from app.models import (
    AuditEvent,
    DocumentFile,
    IdempotencyRecord,
    Invoice,
    ProcessingJob,
    ProcessingOutbox,
    UploadReceipt,
    User,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _counts(app) -> dict[str, int]:
    with app.state.session_factory() as db:
        return {
            "invoices": db.query(Invoice).count(),
            "documents": db.query(DocumentFile).count(),
            "jobs": db.query(ProcessingJob).count(),
            "outbox": db.query(ProcessingOutbox).count(),
            "receipts": db.query(UploadReceipt).count(),
            "idempotency": db.query(IdempotencyRecord).count(),
            "audit": db.query(AuditEvent).count(),
        }


def test_committed_upload_status_returns_durable_invoice_job_and_dispatch_without_mutation(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    dispatched: list[str] = []
    monkeypatch.setattr(
        "app.api.invoices.dispatch_outbox_event",
        lambda _factory, _storage, event_id, **_kwargs: dispatched.append(event_id),
    )
    key = "read-only-committed-status-001"
    pdf = (PROJECT_ROOT / "data" / "demo_docs" / "invoice_matched.pdf").read_bytes()
    upload = api["client"].post(
        "/api/v1/invoices/upload",
        headers={**api["admin"], "Idempotency-Key": key},
        files={"file": ("status-invoice.pdf", pdf, "application/pdf")},
    )
    assert upload.status_code == 202, upload.text
    accepted = upload.json()
    assert len(dispatched) == 1
    before = _counts(api["app"])
    storage_before = set(api["app"].state.storage_dir.iterdir())

    response = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**api["admin"], "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    status_body = response.json()
    assert status_body["status"] == "committed"
    assert status_body["receipt_state"] == "committed"
    assert status_body["invoice_id"] == accepted["invoice_id"]
    assert status_body["job_id"] == accepted["job_id"]
    assert status_body["job_state"] == accepted["job_state"]
    assert status_body["processing_status"] == accepted["processing_status"]
    assert status_body["dispatch_state"] == accepted["dispatch_state"]
    assert status_body["dispatch_attempts"] == accepted["dispatch_attempts"]
    assert response.headers["cache-control"] == "no-store"
    assert "Idempotency-Key" not in response.text
    assert _counts(api["app"]) == before
    assert set(api["app"].state.storage_dir.iterdir()) == storage_before
    assert dispatched == [dispatched[0]], "GET must not re-dispatch or retry the processing event"

    # Missing and another actor's operation are indistinguishable and disclose no IDs.
    cross_user = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**api["reviewer"], "Idempotency-Key": key},
    )
    missing = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**api["reviewer"], "Idempotency-Key": "unknown-operation-key"},
    )
    assert cross_user.status_code == missing.status_code == 200
    assert cross_user.json() == missing.json()
    assert cross_user.json()["status"] == "not_found"
    assert cross_user.json()["invoice_id"] is None


def test_in_progress_upload_status_is_read_only_and_returns_only_receipt_state(api, monkeypatch):
    monkeypatch.setattr(
        "app.api.invoices.dispatch_outbox_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("status lookup dispatched an event")),
    )
    key = "read-only-staged-status-001"
    with api["app"].state.session_factory() as db:
        actor = db.scalar(select(User).where(User.email == "admin@test.local"))
        assert actor is not None
        key_hash = _upload_receipt_key_hash(actor.id, "upload", None, key)
        receipt = UploadReceipt(
            actor_id=actor.id,
            source="upload",
            source_reference=key_hash,
            idempotency_key_hash=key_hash,
            content_sha256=sha256(b"synthetic uncommitted upload").hexdigest(),
            original_filename="pending-invoice.pdf",
            mime_type="application/pdf",
            byte_size=31,
            staging_path=str(api["app"].state.storage_dir / ".pending.uploading"),
            stored_path=str(api["app"].state.storage_dir / "pending.pdf"),
            state="staged",
            updated_at=datetime.now(timezone.utc),
        )
        db.add(receipt)
        db.commit()
        receipt_id = receipt.id
        updated_at = receipt.updated_at.replace(tzinfo=None)
    before = _counts(api["app"])
    storage_before = set(api["app"].state.storage_dir.iterdir())

    response = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**api["admin"], "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress"
    assert response.json()["receipt_state"] == "staged"
    assert response.json()["original_filename"] == "pending-invoice.pdf"
    assert response.json()["invoice_id"] is None
    assert response.json()["job_id"] is None
    assert _counts(api["app"]) == before
    assert set(api["app"].state.storage_dir.iterdir()) == storage_before
    with api["app"].state.session_factory() as db:
        receipt_after = db.get(UploadReceipt, receipt_id)
        assert receipt_after is not None
        assert receipt_after.state == "staged"
        assert receipt_after.updated_at == updated_at


def test_abandoned_receipt_status_does_not_reopen_or_modify_the_operation(api):
    key = "read-only-abandoned-status-001"
    with api["app"].state.session_factory() as db:
        actor = db.scalar(select(User).where(User.email == "admin@test.local"))
        assert actor is not None
        key_hash = _upload_receipt_key_hash(actor.id, "upload", None, key)
        receipt = UploadReceipt(
            actor_id=actor.id,
            source="upload",
            source_reference=key_hash,
            idempotency_key_hash=key_hash,
            content_sha256=sha256(b"synthetic abandoned upload").hexdigest(),
            original_filename="abandoned.pdf",
            mime_type="application/pdf",
            byte_size=28,
            staging_path=str(api["app"].state.storage_dir / ".abandoned.uploading"),
            stored_path=str(api["app"].state.storage_dir / "abandoned.pdf"),
            state="abandoned",
            last_error_type="synthetic_failure",
        )
        db.add(receipt)
        db.commit()
        receipt_id = receipt.id
    before = _counts(api["app"])

    response = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**api["admin"], "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "abandoned"
    assert response.json()["receipt_state"] == "abandoned"
    assert response.json()["invoice_id"] is None
    assert _counts(api["app"]) == before
    with api["app"].state.session_factory() as db:
        assert db.get(UploadReceipt, receipt_id).state == "abandoned"


def test_committed_receipt_without_invoice_requires_reconciliation_without_mutation(api):
    key = "read-only-recovery-required-status-001"
    with api["app"].state.session_factory() as db:
        actor = db.scalar(select(User).where(User.email == "admin@test.local"))
        assert actor is not None
        key_hash = _upload_receipt_key_hash(actor.id, "upload", None, key)
        receipt = UploadReceipt(
            actor_id=actor.id,
            source="upload",
            source_reference=key_hash,
            idempotency_key_hash=key_hash,
            content_sha256=sha256(b"synthetic inconsistent upload").hexdigest(),
            original_filename="recovery-required.pdf",
            mime_type="application/pdf",
            byte_size=35,
            staging_path=str(api["app"].state.storage_dir / ".recovery.uploading"),
            stored_path=str(api["app"].state.storage_dir / "recovery-required.pdf"),
            state="committed",
        )
        db.add(receipt)
        db.commit()
        receipt_id = receipt.id
    before = _counts(api["app"])
    storage_before = set(api["app"].state.storage_dir.iterdir())

    response = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**api["admin"], "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "recovery_required"
    assert response.json()["receipt_state"] == "committed"
    assert response.json()["original_filename"] == "recovery-required.pdf"
    assert response.json()["invoice_id"] is None
    assert response.json()["job_id"] is None
    assert response.json()["dispatch_state"] is None
    assert _counts(api["app"]) == before
    assert set(api["app"].state.storage_dir.iterdir()) == storage_before
    with api["app"].state.session_factory() as db:
        receipt_after = db.get(UploadReceipt, receipt_id)
        assert receipt_after is not None and receipt_after.state == "committed"
