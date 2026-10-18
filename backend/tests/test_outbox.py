from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.config import settings
from app.models import AuditEvent, Invoice, ProcessingJob, ProcessingOutbox, ReviewCase
from app.outbox import (
    OUTBOX_MAX_ATTEMPTS,
    _claim_one,
    _finish_event,
    dispatch_outbox_event,
    enqueue_processing_event,
)


def _queued_job(api, invoice_number: str):
    with api["app"].state.session_factory() as db:
        invoice = Invoice(
            invoice_number=invoice_number, processing_status="queued", workflow_status="processing",
            total_amount=Decimal("25.00"), issue_date=date(2026, 1, 1),
        )
        db.add(invoice)
        db.flush()
        job = ProcessingJob(invoice_id=invoice.id, state="queued", stage="queued")
        db.add(job)
        db.flush()
        event = enqueue_processing_event(db, job_id=job.id, invoice_id=invoice.id, actor_id=None)
        db.commit()
        return invoice.id, job.id, event.id


def test_outbox_claim_token_fences_concurrent_dispatchers(api):
    _invoice_id, _job_id, event_id = _queued_job(api, "OUTBOX-CLAIM-TEST")
    factory = api["app"].state.session_factory
    first = _claim_one(factory, event_id=event_id)
    assert first is not None
    assert first.attempts == 1
    assert _claim_one(factory, event_id=event_id) is None
    with factory() as db:
        event = db.get(ProcessingOutbox, event_id)
        assert event.state == "pending"
        assert event.attempts == 1
        assert event.claim_token == first.claim_token
        assert event.locked_until is not None
    assert _finish_event(factory, first, state="published") is True
    assert _finish_event(factory, first, state="published") is False
    with factory() as db:
        assert db.get(ProcessingOutbox, event_id).state == "published"


def test_outbox_uses_bounded_retry_then_routes_job_to_review(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    monkeypatch.setattr("app.outbox.OUTBOX_MAX_ATTEMPTS", 2)

    def unavailable_queue(_job_id):
        raise ConnectionError("secret broker host and password must not be stored")

    monkeypatch.setattr("app.tasks.process_invoice_task.delay", unavailable_queue)
    invoice_id, job_id, event_id = _queued_job(api, "OUTBOX-RETRY-TEST")
    factory = api["app"].state.session_factory
    storage = api["app"].state.storage_dir

    first_error = dispatch_outbox_event(factory, storage, event_id, force=True)
    assert first_error == "ConnectionError"
    with factory() as db:
        event = db.get(ProcessingOutbox, event_id)
        assert event.state == "pending"
        assert event.attempts == 1
        assert event.last_error_type == "ConnectionError"
        retry_at = event.available_at.replace(tzinfo=timezone.utc) if event.available_at.tzinfo is None else event.available_at
        assert retry_at > datetime.now(timezone.utc)
        event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    second_error = dispatch_outbox_event(factory, storage, event_id)
    assert second_error == "ConnectionError"
    with factory() as db:
        event = db.get(ProcessingOutbox, event_id)
        job = db.get(ProcessingJob, job_id)
        invoice = db.get(Invoice, invoice_id)
        case = db.scalar(select(ReviewCase).where(ReviewCase.invoice_id == invoice_id))
        assert event.state == "dead"
        assert event.attempts == 2
        assert job.state == "failed" and job.stage == "dispatch"
        assert invoice.processing_status == "failed"
        assert invoice.workflow_status == "review_required"
        assert case is not None
        assert "processing_dispatch_failure" in case.reason_codes
        assert "secret broker" not in (job.error_message or "")
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "invoice.processing_dispatch_exhausted"))
        assert audit is not None
        assert audit.details["error_type"] == "ConnectionError"
        assert "password" not in str(audit.details)


def test_retry_limit_is_positive_and_matches_configured_default():
    assert OUTBOX_MAX_ATTEMPTS >= 1


def test_expired_last_claim_is_finalized_without_an_unbounded_ninth_attempt(api):
    from app import outbox as outbox_module

    invoice_id, job_id, event_id = _queued_job(api, "OUTBOX-CRASH-RECOVERY")
    factory = api["app"].state.session_factory
    with factory() as db:
        event = db.get(ProcessingOutbox, event_id)
        event.attempts = outbox_module.OUTBOX_MAX_ATTEMPTS
        event.last_error_type = "TimeoutError"
        event.available_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        event.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        event.claim_token = "expired-claim-token"
        db.commit()

    assert dispatch_outbox_event(factory, api["app"].state.storage_dir, event_id, force=True) is None
    with factory() as db:
        event = db.get(ProcessingOutbox, event_id)
        job = db.get(ProcessingJob, job_id)
        invoice = db.get(Invoice, invoice_id)
        assert event.state == "dead"
        assert event.attempts == outbox_module.OUTBOX_MAX_ATTEMPTS
        assert job.state == "failed" and invoice.workflow_status == "review_required"


def test_local_schema_compatibility_backfills_pre_outbox_queued_jobs(api):
    from app.main import _ensure_local_additive_schema

    with api["app"].state.session_factory() as db:
        invoice = Invoice(invoice_number="OUTBOX-LOCAL-BACKFILL", processing_status="queued", workflow_status="processing")
        db.add(invoice)
        db.flush()
        job = ProcessingJob(invoice_id=invoice.id, state="queued", stage="queued")
        db.add(job)
        db.commit()
        invoice_id, job_id = invoice.id, job.id

    _ensure_local_additive_schema(api["app"].state.engine)
    with api["app"].state.session_factory() as db:
        event = db.scalar(select(ProcessingOutbox).where(ProcessingOutbox.job_id == job_id))
        assert event is not None
        assert event.invoice_id == invoice_id
        assert event.state == "pending" and event.attempts == 0
