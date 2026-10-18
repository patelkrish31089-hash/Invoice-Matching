from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import hashlib
import hmac
from pathlib import Path
import time
import uuid

import fitz
import pytest
from sqlalchemy import select

from app import config
from app.models import (
    DocumentFile, EmailIngestionEvent, Invoice, ProcessingJob, ProcessingOutbox,
    QuarantinedUpload, UploadReceipt,
)


_SECRET = "test-only-email-webhook-secret-0123456789"
_ALLOWED = "ap@example.test"


def _pdf_bytes(text: str = "Synthetic invoice") -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def _message(filename: str = "invoice.pdf", payload: bytes | None = None) -> bytes:
    if payload is None:
        payload = _pdf_bytes()
    message = EmailMessage()
    message["From"] = "AP Team <ap@example.test>"
    message["To"] = "invoices@example.test"
    message["Subject"] = "Synthetic invoice submission"
    message["Message-ID"] = "<synthetic-event@example.test>"
    message.set_content("Please process the attached synthetic invoice.")
    message.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    return message.as_bytes()


def _headers(raw: bytes, event_id: str = "provider-event-001", *, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    signing_input = timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + raw
    signature = hmac.new(_SECRET.encode(), signing_input, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "message/rfc822",
        "X-InvoiceGuard-Event-ID": event_id,
        "X-InvoiceGuard-Timestamp": timestamp,
        "X-InvoiceGuard-Signature": f"sha256={signature}",
    }


def _configure(monkeypatch):
    monkeypatch.setattr(config.settings, "email_ingest_webhook_secret", _SECRET)
    monkeypatch.setattr(config.settings, "email_ingest_allowed_senders", _ALLOWED)
    monkeypatch.setattr(config.settings, "email_ingest_max_message_mb", 25)
    monkeypatch.setattr(config.settings, "email_ingest_signature_tolerance_seconds", 300)


def test_signed_email_ingestion_is_deduplicated_and_queues_existing_pipeline(api, monkeypatch):
    from app.api import email_ingestion

    _configure(monkeypatch)
    dispatches = []
    monkeypatch.setattr(email_ingestion, "dispatch_outbox_event", lambda *_args, **kwargs: dispatches.append(kwargs.get("force")))
    raw = _message()
    headers = _headers(raw)

    first = api["client"].post("/api/v1/ingestion/email", content=raw, headers=headers)
    assert first.status_code == 202, first.text
    first_payload = first.json()
    assert first_payload["source"] == "email"
    assert first_payload["attachments_received"] == 1
    assert first_payload["payment_execution"] is False
    assert first_payload["items"][0]["job_state"] == "queued"

    replay = api["client"].post("/api/v1/ingestion/email", content=raw, headers=headers)
    assert replay.status_code == 202, replay.text
    replay_payload = replay.json()
    assert replay_payload["duplicate_event"] is True
    assert replay_payload["items"][0]["invoice_id"] == first_payload["items"][0]["invoice_id"]
    assert dispatches == [True]

    with api["app"].state.session_factory() as db:
        assert db.scalar(select(EmailIngestionEvent).where(EmailIngestionEvent.provider_event_id == "provider-event-001")).state == "completed"
        invoice = db.get(Invoice, first_payload["items"][0]["invoice_id"])
        assert invoice.source == "email" and invoice.workflow_status == "processing"
        assert db.scalar(select(DocumentFile).where(DocumentFile.invoice_id == invoice.id)) is not None
        assert db.scalar(select(ProcessingJob).where(ProcessingJob.invoice_id == invoice.id)) is not None
        assert db.scalar(select(ProcessingOutbox).where(ProcessingOutbox.invoice_id == invoice.id)) is not None
        assert db.query(Invoice).filter_by(source="email").count() == 1
        assert db.query(UploadReceipt).filter_by(source="email").one().state == "committed"


def test_email_event_id_cannot_be_reused_for_different_signed_content(api, monkeypatch):
    from app.api import email_ingestion

    _configure(monkeypatch)
    monkeypatch.setattr(email_ingestion, "dispatch_outbox_event", lambda *_args, **_kwargs: None)
    original = _message(payload=_pdf_bytes("Synthetic invoice A"))
    changed = _message(payload=_pdf_bytes("Synthetic invoice B"))
    first = api["client"].post("/api/v1/ingestion/email", content=original, headers=_headers(original, "same-event"))
    second = api["client"].post("/api/v1/ingestion/email", content=changed, headers=_headers(changed, "same-event"))
    assert first.status_code == 202
    assert second.status_code == 409
    with api["app"].state.session_factory() as db:
        assert db.query(Invoice).filter_by(source="email").count() == 1


def test_email_ingestion_rejects_missing_configuration_before_body_processing(api, monkeypatch):
    from app.api import email_ingestion

    monkeypatch.setattr(config.settings, "email_ingest_webhook_secret", None)
    monkeypatch.setattr(config.settings, "email_ingest_allowed_senders", "")
    monkeypatch.setattr(email_ingestion, "_read_limited_body", lambda *_args, **_kwargs: pytest.fail("body should not be read"))

    response = api["client"].post("/api/v1/ingestion/email", content=b"untrusted", headers={"X-InvoiceGuard-Event-ID": "event-1"})
    assert response.status_code == 503
    with api["app"].state.session_factory() as db:
        assert db.query(EmailIngestionEvent).count() == 0


def test_email_ingestion_rejects_invalid_signature_sender_and_file(api, monkeypatch):
    _configure(monkeypatch)
    raw = _message()
    headers = _headers(raw)
    headers["X-InvoiceGuard-Signature"] = "sha256=" + "0" * 64
    bad_signature = api["client"].post("/api/v1/ingestion/email", content=raw, headers=headers)
    assert bad_signature.status_code == 401

    other_sender = EmailMessage()
    other_sender["From"] = "attacker@example.test"
    other_sender["To"] = "invoices@example.test"
    other_sender.set_content("not allowed")
    other_sender.add_attachment(b"%PDF-1.7\\nnope", maintype="application", subtype="pdf", filename="invoice.pdf")
    disallowed = other_sender.as_bytes()
    sender_response = api["client"].post("/api/v1/ingestion/email", content=disallowed, headers=_headers(disallowed, "sender-event"))
    assert sender_response.status_code == 403

    invalid_file = _message(payload=b"not a pdf")
    file_response = api["client"].post("/api/v1/ingestion/email", content=invalid_file, headers=_headers(invalid_file, "file-event"))
    assert file_response.status_code == 415
    with api["app"].state.session_factory() as db:
        assert db.query(EmailIngestionEvent).count() == 0
        assert db.query(Invoice).filter_by(source="email").count() == 0


def test_email_ingestion_rejects_expired_signature_and_oversize_message(api, monkeypatch):
    _configure(monkeypatch)
    raw = _message()
    expired = str(int(time.time()) - 1000)
    expired_response = api["client"].post("/api/v1/ingestion/email", content=raw, headers=_headers(raw, "expired-event", timestamp=expired))
    assert expired_response.status_code == 401

    monkeypatch.setattr(config.settings, "email_ingest_max_message_mb", 1)
    too_large = b"x" * (1024 * 1024 + 1)
    oversized = api["client"].post("/api/v1/ingestion/email", content=too_large, headers={"X-InvoiceGuard-Event-ID": "large-event"})
    assert oversized.status_code == 413
    with api["app"].state.session_factory() as db:
        assert db.query(EmailIngestionEvent).count() == 0


def test_email_ingestion_rolls_back_files_and_rows_when_transaction_fails(api, monkeypatch):
    from app.api import email_ingestion

    _configure(monkeypatch)
    monkeypatch.setattr(email_ingestion, "enqueue_processing_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic db failure")))
    raw = _message()
    response = api["client"].post("/api/v1/ingestion/email", content=raw, headers=_headers(raw, "rollback-event"))
    assert response.status_code == 500
    with api["app"].state.session_factory() as db:
        event = db.query(EmailIngestionEvent).filter_by(provider_event_id="rollback-event").one()
        assert event.state == "failed"
        assert db.query(Invoice).filter_by(source="email").count() == 0
        assert db.query(UploadReceipt).filter_by(source="email").one().state == "abandoned"
    assert list(Path(api["app"].state.storage_dir).iterdir()) == []


def test_email_malware_attachment_is_quarantined_without_invoice(api, monkeypatch):
    from app.api import email_ingestion
    from app.services.malware_scan import MalwareScanResult

    _configure(monkeypatch)
    scan = MalwareScanResult(
        status="infected", engine="ClamAV", engine_version="test-engine",
        signature="Synthetic-Email-Detection",
    )
    monkeypatch.setattr(email_ingestion, "scan_submission", lambda _payload: scan)
    payload = _pdf_bytes("Synthetic infected email attachment")
    raw = _message(filename="infected.pdf", payload=payload)
    headers = _headers(raw, "infected-email-event")

    response = api["client"].post("/api/v1/ingestion/email", content=raw, headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["attachments_received"] == 1
    assert body["queued_attachments"] == 0
    assert body["quarantined_attachments"] == 1
    assert body["payment_execution"] is False

    with api["app"].state.session_factory() as db:
        assert db.query(Invoice).filter_by(source="email").count() == 0
        assert db.query(UploadReceipt).filter_by(source="email").count() == 0
        quarantine = db.query(QuarantinedUpload).filter_by(source="email").one()
        event = db.query(EmailIngestionEvent).filter_by(provider_event_id="infected-email-event").one()
        assert event.state == "completed"
        quarantine_path = Path(quarantine.quarantine_path)
        assert quarantine_path.read_bytes() == payload
    assert list(Path(api["app"].state.storage_dir).iterdir()) == []

    replay = api["client"].post("/api/v1/ingestion/email", content=raw, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["duplicate_event"] is True
    with api["app"].state.session_factory() as db:
        assert db.query(QuarantinedUpload).filter_by(source="email").count() == 1


def test_stale_email_event_and_receipt_recover_then_resume_idempotently(api, monkeypatch):
    from app.api import email_ingestion

    _configure(monkeypatch)
    monkeypatch.setattr(email_ingestion, "dispatch_outbox_event", lambda *_args, **_kwargs: None)
    event_id = "crash-recovery-email"
    payload = _pdf_bytes("Synthetic crash recovery invoice")
    raw = _message(payload=payload)
    old_time = datetime.now(timezone.utc) - timedelta(hours=3)
    storage_root = Path(api["app"].state.storage_dir)
    receipt_id = str(uuid.uuid4())
    old_invoice_id = str(uuid.uuid4())
    old_stage = storage_root / f".{receipt_id}.uploading"
    old_final = storage_root / f"{old_invoice_id}.pdf"
    old_stage.write_bytes(payload)
    old_final.write_bytes(payload)

    with api["app"].state.session_factory() as db:
        db.add(EmailIngestionEvent(
            provider_event_id=event_id,
            payload_sha256=hashlib.sha256(raw).hexdigest(),
            state="received",
            response_body={"event_state": "received"},
            created_at=old_time,
        ))
        db.add(UploadReceipt(
            id=receipt_id,
            source="email",
            source_reference=hashlib.sha256(f"email:{event_id}:0".encode()).hexdigest(),
            actor_id=None,
            idempotency_key_hash=None,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            original_filename="invoice.pdf",
            mime_type="application/pdf",
            byte_size=len(payload),
            staging_path=str(old_stage),
            stored_path=str(old_final),
            state="staged",
            created_at=old_time,
            updated_at=old_time,
        ))
        db.commit()

    response = api["client"].post("/api/v1/ingestion/email", content=raw, headers=_headers(raw, event_id))
    assert response.status_code == 202, response.text
    assert not old_stage.exists() and not old_final.exists()
    item = response.json()["items"][0]
    with api["app"].state.session_factory() as db:
        document = db.scalar(select(DocumentFile).where(DocumentFile.invoice_id == item["invoice_id"]))
        event = db.query(EmailIngestionEvent).filter_by(provider_event_id=event_id).one()
        receipt = db.get(UploadReceipt, receipt_id)
        assert event.state == "completed"
        assert receipt.state == "committed"
        assert receipt.invoice_id == item["invoice_id"]
        assert db.query(Invoice).filter_by(source="email").count() == 1
        new_path = Path(document.stored_path)
    assert new_path.is_file() and new_path.read_bytes() == payload

    replay = api["client"].post("/api/v1/ingestion/email", content=raw, headers=_headers(raw, event_id))
    assert replay.status_code == 202
    assert replay.json()["duplicate_event"] is True
