from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
import csv
from io import StringIO

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DocumentFile, ExtractionResult, FraudAlert, FraudAssessment, IdempotencyRecord, Invoice, InvoiceMatchResult, ModelFeedback, ProcessingJob, PurchaseOrder, ReviewCase, User, Vendor
from app.processing import _finish_processing_job, _prior_invoice_quantities, evaluate_invoice


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_login_role_protection_and_audit(api):
    client: TestClient = api["client"]
    assert client.get("/api/v1/invoices").status_code == 401
    assert client.get("/api/v1/admin/users", headers=api["admin"]).status_code == 200
    assert client.get("/api/v1/admin/users", headers=api["auditor"]).status_code == 403
    check = client.get("/api/v1/admin/audit-logs/verify", headers=api["admin"])
    assert check.status_code == 200
    assert check.json()["valid"] is True


def test_purchase_order_and_goods_receipt_crud_is_audited(api):
    client: TestClient = api["client"]
    headers = api["admin"]
    vendor_response = client.post("/api/v1/vendors", headers=headers, json={"code": "TEST-01", "name": "Test Components Ltd", "aliases": [], "risk_tier": "standard"})
    assert vendor_response.status_code == 201, vendor_response.text
    vendor_id = vendor_response.json()["id"]
    po_response = client.post("/api/v1/purchase-orders", headers=headers, json={
        "number": "PO-TEST-01", "vendor_id": vendor_id, "currency": "USD", "issue_date": "2026-10-01",
        "lines": [{"line_number": 1, "sku": "TEST-1", "description": "Test component", "quantity": "4", "unit_price": "12.50", "tax_rate": "0"}],
    })
    assert po_response.status_code == 201, po_response.text
    po = po_response.json()
    assert po["expected_total"] == "50.00"
    grn_response = client.post("/api/v1/goods-receipts", headers=headers, json={
        "number": "GRN-TEST-01", "purchase_order_id": po["id"], "received_date": "2026-10-02",
        "lines": [{"purchase_order_line_id": po["lines"][0]["id"], "description": "Test component", "sku": "TEST-1", "quantity": "2"}],
    })
    assert grn_response.status_code == 201, grn_response.text
    assert float(grn_response.json()["lines"][0]["quantity"]) == 2.0
    audit = client.get("/api/v1/admin/audit-logs/verify", headers=headers)
    assert audit.status_code == 200 and audit.json()["valid"] is True


def test_admin_user_lifecycle_and_settings(api, monkeypatch):
    client: TestClient = api["client"]
    headers = api["admin"]
    created = client.post("/api/v1/admin/users", headers=headers, json={
        "email": "new-clerk@test.local", "display_name": "New Clerk", "password": "StrongClerkPass123!", "roles": ["ap_clerk"],
    })
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    updated = client.put(f"/api/v1/admin/users/{user_id}/roles", headers=headers, json={"roles": ["reviewer", "auditor"]})
    assert updated.status_code == 200
    assert updated.json()["roles"] == ["auditor", "reviewer"]
    inactive = client.patch(f"/api/v1/admin/users/{user_id}/active?active=false", headers=headers)
    assert inactive.status_code == 200 and inactive.json()["active"] is False
    updated_settings = client.patch("/api/v1/admin/settings", headers=headers, json={"tolerance_absolute": "0.10", "tolerance_policy": "either", "benford_min_sample": 200})
    assert updated_settings.status_code == 200, updated_settings.text
    assert updated_settings.json()["tolerance_policy"] == "either"
    assert updated_settings.json()["auto_payment_enabled"] is False
    # Simulate an API process restart: persisted policy must win over untouched environment defaults.
    monkeypatch.setattr(settings, "tolerance_absolute", "0.05")
    monkeypatch.setattr(settings, "tolerance_policy", "stricter")
    reconciliation = client.get("/api/v1/analytics/reconciliation", headers=headers)
    assert reconciliation.json()["tolerance_policy"] == {"absolute": "0.10", "percentage": "0.005", "mode": "either"}
    fraud_metrics = client.get("/api/v1/analytics/fraud", headers=headers)
    assert fraud_metrics.json()["benford_policy"]["minimum_sample"] == 200


def test_unauthenticated_upload_creates_no_persistent_invoice_job_or_storage_file(api):
    client = api["client"]
    storage_dir = api["app"].state.storage_dir
    pdf = b"%PDF-1.7\nSynthetic unauthenticated upload test\n%%EOF"
    with api["app"].state.session_factory() as db:
        before = {
            "invoices": db.query(Invoice).count(),
            "documents": db.query(DocumentFile).count(),
            "jobs": db.query(ProcessingJob).count(),
            "idempotency": db.query(IdempotencyRecord).count(),
        }
    files_before = set(storage_dir.iterdir())

    response = client.post(
        "/api/v1/invoices/upload", headers={"Idempotency-Key": "unauth-upload-001"},
        files={"file": ("synthetic.pdf", pdf, "application/pdf")},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Bearer token required"
    with api["app"].state.session_factory() as db:
        after = {
            "invoices": db.query(Invoice).count(),
            "documents": db.query(DocumentFile).count(),
            "jobs": db.query(ProcessingJob).count(),
            "idempotency": db.query(IdempotencyRecord).count(),
        }
    assert after == before
    assert set(storage_dir.iterdir()) == files_before


def test_secure_upload_preview_and_idempotency(api):
    client: TestClient = api["client"]
    headers = api["admin"]
    pdf = (PROJECT_ROOT / "data" / "demo_docs" / "invoice_matched.pdf").read_bytes()
    first = client.post("/api/v1/invoices/upload", headers={**headers, "Idempotency-Key": "test-upload-001"}, files={"file": ("matched.pdf", pdf, "application/pdf")})
    assert first.status_code == 202, first.text
    result = first.json()
    cross_user = client.post("/api/v1/invoices/upload", headers={**api["reviewer"], "Idempotency-Key": "test-upload-001"}, files={"file": ("matched.pdf", pdf, "application/pdf")})
    assert cross_user.status_code == 409
    second = client.post("/api/v1/invoices/upload", headers={**headers, "Idempotency-Key": "test-upload-001"}, files={"file": ("matched.pdf", pdf, "application/pdf")})
    assert second.status_code == 202
    assert second.json()["invoice_id"] == result["invoice_id"]
    assert second.json()["job_state"] == "completed"
    assert second.json()["processing_status"] == "completed"
    storage_dir = api["app"].state.storage_dir
    with api["app"].state.session_factory() as db:
        invoice_count = db.query(Invoice).count()
        document_count = db.query(DocumentFile).count()
        job_count = db.query(ProcessingJob).count()
    files_before_conflict = set(storage_dir.iterdir())
    conflicting = client.post(
        "/api/v1/invoices/upload", headers={**headers, "Idempotency-Key": "test-upload-001"},
        files={"file": ("different.pdf", pdf + b"\n% changed bytes", "application/pdf")},
    )
    assert conflicting.status_code == 409
    assert "different file content" in conflicting.json()["detail"]
    with api["app"].state.session_factory() as db:
        assert db.query(Invoice).count() == invoice_count
        assert db.query(DocumentFile).count() == document_count
        assert db.query(ProcessingJob).count() == job_count
    assert set(storage_dir.iterdir()) == files_before_conflict
    detail = client.get(f"/api/v1/invoices/{result['invoice_id']}", headers=headers)
    assert detail.status_code == 200
    field = detail.json()["extraction"]["fields"]["invoice_number"]
    assert field["value"] == "INV-ACM-8841"
    assert field["source_page"] == 1 and field["bbox"]
    assert "not a trained model probability" in field["confidence_basis"]
    preview = client.get(f"/api/v1/invoices/{result['invoice_id']}/document/pages/1", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
    traversal = client.get(f"/api/v1/invoices/{result['invoice_id']}/document/pages/99", headers=headers)
    assert traversal.status_code == 404


def test_normal_upload_idempotency_survives_later_vendor_resolution(api):
    client = api["client"]
    vendor_response = client.post("/api/v1/vendors", headers=api["admin"], json={
        "code": "REPLAY-RESOLVE-01", "name": "Synthetic Replay Resolution Vendor",
        "aliases": ["Acme Systems LLC"], "risk_tier": "standard",
    })
    assert vendor_response.status_code == 201, vendor_response.text
    vendor_id = vendor_response.json()["id"]

    pdf = (PROJECT_ROOT / "data" / "demo_docs" / "invoice_matched.pdf").read_bytes()
    key = "normal-upload-vendor-resolution-001"
    headers = {**api["admin"], "Idempotency-Key": key}
    first = client.post(
        "/api/v1/invoices/upload", headers=headers,
        files={"file": ("vendor-resolved.pdf", pdf, "application/pdf")},
    )
    assert first.status_code == 202, first.text
    invoice_id = first.json()["invoice_id"]

    # Model the later worker update that enriches a normal upload with a resolved vendor.
    with api["app"].state.session_factory() as db:
        invoice = db.get(Invoice, invoice_id)
        assert invoice is not None and invoice.source == "upload"
        invoice.vendor_id = vendor_id
        db.commit()

    replay = client.post(
        "/api/v1/invoices/upload", headers=headers,
        files={"file": ("vendor-resolved.pdf", pdf, "application/pdf")},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["invoice_id"] == invoice_id
    with api["app"].state.session_factory() as db:
        assert db.query(Invoice).count() == 1
        assert db.query(DocumentFile).count() == 1
        assert db.get(Invoice, invoice_id).vendor_id == vendor_id


def test_upload_idempotency_replay_does_not_redispatch_a_published_message(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    dispatched = []
    monkeypatch.setattr("app.tasks.process_invoice_task.delay", lambda job_id: dispatched.append(job_id))
    pdf = (PROJECT_ROOT / "data" / "demo_docs" / "invoice_matched.pdf").read_bytes()
    headers = {**api["admin"], "Idempotency-Key": "queued-replay-001"}
    first = api["client"].post("/api/v1/invoices/upload", headers=headers, files={"file": ("queued-replay.pdf", pdf, "application/pdf")})
    assert first.status_code == 202, first.text
    assert first.json()["job_state"] == "queued"
    replay = api["client"].post("/api/v1/invoices/upload", headers=headers, files={"file": ("queued-replay.pdf", pdf, "application/pdf")})
    assert replay.status_code == 202, replay.text
    assert replay.json()["invoice_id"] == first.json()["invoice_id"]
    assert replay.json()["job_state"] == "queued"
    assert dispatched == [first.json()["job_id"]]


def test_idempotency_commit_race_replays_winner_and_removes_losing_file(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    dispatched = []
    monkeypatch.setattr("app.tasks.process_invoice_task.delay", lambda job_id: dispatched.append(job_id))
    pdf = b"%PDF-1.7\nSynthetic concurrent idempotency request\n%%EOF"
    digest = __import__("hashlib").sha256(pdf).hexdigest()
    key = "concurrent-upload-key-001"
    storage_dir = api["app"].state.storage_dir
    with api["app"].state.session_factory() as db:
        actor_id = db.query(User).filter_by(email="admin@test.local").one().id

    original_commit = Session.commit
    race = {"triggered": False, "winner_invoice_id": None, "winner_job_id": None}

    def simulate_unique_key_race(session):
        if not race["triggered"] and any(isinstance(item, IdempotencyRecord) and item.key == key for item in session.new):
            race["triggered"] = True
            # The losing request has written its UUID file but not committed its DB rows.
            session.rollback()
            with api["app"].state.session_factory() as winner:
                invoice = Invoice(
                    invoice_number="PENDING-RACE-WINNER", source="upload",
                    processing_status="queued", workflow_status="processing", is_synthetic=False,
                )
                winner.add(invoice)
                winner.flush()
                winner_path = storage_dir / f"{invoice.id}.pdf"
                winner_path.write_bytes(pdf)
                winner.add_all([
                    DocumentFile(
                        invoice_id=invoice.id, original_filename="race-winner.pdf",
                        stored_path=str(winner_path.resolve()), mime_type="application/pdf",
                        sha256=digest, byte_size=len(pdf),
                    ),
                    ProcessingJob(invoice_id=invoice.id, state="queued", stage="queued"),
                ])
                winner.flush()
                job = winner.query(ProcessingJob).filter_by(invoice_id=invoice.id).one()
                response_body = {
                    "invoice_id": invoice.id, "invoice_number": invoice.invoice_number,
                    "job_id": job.id, "job_state": "queued", "job_stage": "queued",
                    "processing_status": "queued", "workflow_status": "processing",
                    "filename": "race-winner.pdf", "sha256": digest,
                    "message": "Document saved. Processing is queued; status will update separately. No payment will be initiated.",
                }
                winner.add(IdempotencyRecord(key=key, operation="invoice_upload", actor_id=actor_id, response_body=response_body))
                winner.commit()
                race["winner_invoice_id"], race["winner_job_id"] = invoice.id, job.id
            raise IntegrityError("simulated unique idempotency-key conflict", {}, Exception("duplicate key"))
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", simulate_unique_key_race)
    response = api["client"].post(
        "/api/v1/invoices/upload", headers={**api["admin"], "Idempotency-Key": key},
        files={"file": ("race-loser.pdf", pdf, "application/pdf")},
    )
    assert response.status_code == 202, response.text
    assert response.json()["invoice_id"] == race["winner_invoice_id"]
    assert response.json()["job_id"] == race["winner_job_id"]
    assert dispatched == [race["winner_job_id"]]
    with api["app"].state.session_factory() as db:
        assert db.query(Invoice).count() == 1
        assert db.query(DocumentFile).count() == 1
        assert db.query(ProcessingJob).count() == 1
        assert db.query(IdempotencyRecord).count() == 1
    assert {path.name for path in storage_dir.iterdir()} == {f"{race['winner_invoice_id']}.pdf"}


def test_upload_rejects_non_document_payload(api):
    client: TestClient = api["client"]
    invalid = client.post("/api/v1/invoices/upload", headers=api["admin"], files={"file": ("not-a-pdf.pdf", b"not a PDF", "application/pdf")})
    assert invalid.status_code in {400, 415}



def test_review_queue_uses_priority_rank_not_lexicographic_order(api):
    db = api["app"].state.session_factory()
    for priority in ("normal", "critical", "high"):
        invoice = Invoice(invoice_number=f"INV-{priority.upper()}", source="test", processing_status="completed", workflow_status="review_required")
        db.add(invoice)
        db.flush()
        case = ReviewCase(invoice_id=invoice.id, status="open", priority=priority, reason_codes=["test"], summary="test case")
        db.add(case)
        db.flush()
    db.commit()
    db.close()
    result = api["client"].get("/api/v1/review/cases?limit=10", headers=api["admin"])
    assert result.status_code == 200
    assert [item["priority"] for item in result.json()["items"]] == ["critical", "high", "normal"]


def test_exposure_analytics_never_adds_mixed_currencies(api):
    db = api["app"].state.session_factory()
    for invoice_number, currency_code, amount in (("INV-USD", "USD", "10.00"), ("INV-EUR", "EUR", "20.00")):
        invoice = Invoice(invoice_number=invoice_number, currency=currency_code, source="test", processing_status="completed", workflow_status="review_required")
        db.add(invoice)
        db.flush()
        db.add(InvoiceMatchResult(invoice_id=invoice.id, outcome="MISMATCH", details={"financial_exposure": amount}))
        db.add(FraudAlert(invoice_id=invoice.id, alert_type="test", severity=0.7, title="Test alert", explanation="Synthetic test", evidence={"estimated_exposure": amount}, status="open"))
    db.commit()
    db.close()
    overview = api["client"].get("/api/v1/analytics/overview", headers=api["admin"]).json()
    assert overview["estimated_discrepancy_exposure"] is None
    assert overview["estimated_discrepancy_exposure_by_currency"] == {"EUR": "20.00", "USD": "10.00"}
    fraud = api["client"].get("/api/v1/analytics/fraud", headers=api["admin"]).json()
    assert fraud["estimated_alert_exposure"] is None
    assert fraud["estimated_alert_exposure_by_currency"] == {"EUR": "20.00", "USD": "10.00"}


@pytest.mark.parametrize(
    "extracted_fields",
    [
        {"currency": {"value": "USD", "normalized_value": "USD", "confidence": 0.4}},
        {},
    ],
    ids=["low-confidence", "missing-currency"],
)
def test_low_or_missing_currency_confidence_requires_manual_review(api, extracted_fields):
    db = api["app"].state.session_factory()
    invoice = Invoice(
        invoice_number="INV-CURRENCY-CONFIDENCE", source="test", processing_status="completed",
        workflow_status="processing", currency="USD", total_amount=Decimal("12.00"),
        extraction_confidence=1.0,
    )
    db.add(invoice)
    db.flush()
    db.add(ExtractionResult(
        invoice_id=invoice.id, extraction_version=1, extracted_fields=extracted_fields,
        validation_errors=[], tokens=[], engine_name="test", engine_version="test-1",
    ))
    db.flush()
    result = evaluate_invoice(db, invoice)
    db.commit()
    db.close()
    assert "currency_confidence_below_policy" in result["review_reasons"]


def test_fraud_analytics_does_not_truncate_at_one_thousand_open_alerts(api):
    db = api["app"].state.session_factory()
    invoice = Invoice(invoice_number="INV-MANY-ALERTS", currency="USD", source="test", processing_status="completed")
    db.add(invoice)
    db.flush()
    db.add_all([
        FraudAlert(
            invoice_id=invoice.id, alert_type="synthetic_test", severity=0.5,
            title=f"Test alert {index}", explanation="Synthetic regression test",
            evidence={"estimated_exposure": "1.00"}, status="open",
        )
        for index in range(1005)
    ])
    db.commit()
    db.close()
    response = api["client"].get("/api/v1/analytics/fraud", headers=api["admin"])
    assert response.status_code == 200
    assert response.json()["open_alerts"] == 1005



def test_performance_analytics_uses_aggregate_counts_and_bounded_latency_sample(api):
    db = api["app"].state.session_factory()
    invoice = Invoice(invoice_number="INV-PERFORMANCE-SAMPLE", source="test")
    db.add(invoice)
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all([
        ProcessingJob(
            invoice_id=invoice.id, state="completed", stage="complete",
            started_at=now - timedelta(seconds=2), finished_at=now - timedelta(seconds=1),
        )
        for _ in range(1001)
    ])
    db.add_all([
        ProcessingJob(invoice_id=invoice.id, state="failed", stage="document_extraction", started_at=now - timedelta(seconds=4), finished_at=now - timedelta(seconds=3)),
        ProcessingJob(invoice_id=invoice.id, state="queued", stage="queued"),
        ProcessingJob(invoice_id=invoice.id, state="processing", stage="document_extraction"),
    ])
    db.commit()
    db.close()

    response = api["client"].get("/api/v1/analytics/performance", headers=api["admin"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["job_count"] == 1004
    assert result["completed_jobs"] == 1001
    assert result["failed_jobs"] == 1
    assert result["queued_jobs"] == 1
    assert result["processing_jobs"] == 1
    assert result["latency_sample_size"] == result["latency_sample_limit"] == 1000
    assert result["median_processing_ms"] == 1000.0
    assert "at most the latest 1,000" in result["latency_note"]


def test_upload_dispatch_failure_is_retryable_and_admin_can_recover_outbox_event(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    broker = {"fail": True, "calls": 0}

    def simulated_queue(_job_id):
        broker["calls"] += 1
        if broker["fail"]:
            raise ConnectionError("private broker address and credentials")

    monkeypatch.setattr("app.tasks.process_invoice_task.delay", simulated_queue)
    pdf = (PROJECT_ROOT / "data" / "demo_docs" / "invoice_matched.pdf").read_bytes()
    headers = {**api["admin"], "Idempotency-Key": "dispatch-failure-001"}
    response = api["client"].post(
        "/api/v1/invoices/upload", headers=headers,
        files={"file": ("dispatch-failure.pdf", pdf, "application/pdf")},
    )
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["invoice_id"] and result["job_id"]
    assert result["job_state"] == "queued"
    assert result["processing_status"] == "queued"
    assert result["dispatch_state"] == "pending"
    assert result["dispatch_attempts"] == 1
    assert "dispatch_warning" in result
    assert "private broker address" not in response.text

    detail = api["client"].get(f"/api/v1/invoices/{result['invoice_id']}", headers=api["admin"])
    assert detail.status_code == 200
    assert detail.json()["job"]["state"] == "queued"
    assert detail.json()["job"]["dispatch_state"] == "pending"
    assert detail.json()["job"]["dispatch_error_type"] == "ConnectionError"
    assert detail.json()["review_case"] is None

    denied = api["client"].get("/api/v1/admin/processing-outbox", headers=api["reviewer"])
    assert denied.status_code == 403
    outbox = api["client"].get("/api/v1/admin/processing-outbox", headers=api["admin"])
    assert outbox.status_code == 200, outbox.text
    event = next(item for item in outbox.json()["items"] if item["job_id"] == result["job_id"])
    assert event["state"] == "pending" and event["attempts"] == 1
    assert event["last_error_type"] == "ConnectionError"

    # A same-key replay must not bypass the persisted backoff or change its identity.
    replay = api["client"].post(
        "/api/v1/invoices/upload", headers=headers,
        files={"file": ("dispatch-failure.pdf", pdf, "application/pdf")},
    )
    assert replay.status_code == 202
    assert replay.json()["invoice_id"] == result["invoice_id"]
    assert replay.json()["job_state"] == "queued"
    assert replay.json()["dispatch_attempts"] == 1
    assert broker["calls"] == 1

    broker["fail"] = False
    retry = api["client"].post(
        f"/api/v1/admin/processing-outbox/{event['id']}/retry", headers=api["admin"],
        json={"reason": "Broker health was checked; the local test queue is available."},
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["dispatch_state"] == "published"
    assert retry.json()["job_state"] == "queued"
    assert broker["calls"] == 2
    repeated = api["client"].post(
        "/api/v1/invoices/upload", headers=headers,
        files={"file": ("dispatch-failure.pdf", pdf, "application/pdf")},
    )
    assert repeated.status_code == 202
    assert repeated.json()["dispatch_state"] == "published"
    assert broker["calls"] == 2


def test_stale_job_recovery_is_admin_only_audited_and_fences_late_worker(api):
    db = api["app"].state.session_factory()
    now = datetime.now(timezone.utc)
    invoice = Invoice(
        invoice_number="INV-STALE-RECOVERY", source="test",
        processing_status="processing", workflow_status="processing",
    )
    db.add(invoice)
    db.flush()
    job = ProcessingJob(
        invoice_id=invoice.id, state="processing", stage="document_extraction", attempts=1,
        queued_at=now - timedelta(hours=3), started_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(hours=2),
    )
    db.add(job)
    db.commit()
    invoice_id, job_id = invoice.id, job.id
    db.close()

    client = api["client"]
    denied = client.get("/api/v1/admin/processing-jobs/stale", headers=api["reviewer"])
    assert denied.status_code == 403
    stale = client.get("/api/v1/admin/processing-jobs/stale?queued_minutes=10&processing_minutes=60", headers=api["admin"])
    assert stale.status_code == 200, stale.text
    item = next(item for item in stale.json()["items"] if item["job_id"] == job_id)
    assert item["state"] == "processing"
    assert item["stale_after_minutes"] == 60
    assert item["age_seconds"] > 60 * 60
    assert stale.json()["automatic_recovery"] is False

    recovered = client.post(
        f"/api/v1/admin/processing-jobs/{job_id}/recover", headers=api["admin"],
        json={"expected_state": "processing", "stale_after_minutes": 60, "reason": "Worker host was verified unavailable."},
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["state"] == "failed"

    # A worker that returns after explicit stale recovery must not overwrite the audited failure.
    db = api["app"].state.session_factory()
    assert _finish_processing_job(db, job_id, 1, state="completed", stage="complete") is False
    db.rollback()
    failed_job = db.get(ProcessingJob, job_id)
    failed_invoice = db.get(Invoice, invoice_id)
    assert failed_job.state == "failed"
    assert failed_job.stage == "stale_recovered"
    assert failed_invoice.processing_status == "failed"
    assert failed_invoice.workflow_status == "review_required"
    review_cases = db.query(ReviewCase).filter(ReviewCase.invoice_id == invoice_id).all()
    assert any("stale_processing_job" in case.reason_codes for case in review_cases)
    db.close()

    replay = client.post(
        f"/api/v1/admin/processing-jobs/{job_id}/recover", headers=api["admin"],
        json={"expected_state": "processing", "stale_after_minutes": 60, "reason": "Duplicate recovery attempt."},
    )
    assert replay.status_code == 409
    integrity = client.get("/api/v1/admin/audit-logs/verify", headers=api["admin"])
    assert integrity.status_code == 200 and integrity.json()["valid"] is True


def test_stale_queued_jobs_are_listed_and_recent_jobs_cannot_be_recovered(api):
    db = api["app"].state.session_factory()
    now = datetime.now(timezone.utc)
    stale_invoice = Invoice(invoice_number="INV-STALE-QUEUED", source="test", processing_status="queued", workflow_status="processing")
    recent_invoice = Invoice(invoice_number="INV-RECENT-QUEUED", source="test", processing_status="queued", workflow_status="processing")
    db.add_all([stale_invoice, recent_invoice])
    db.flush()
    stale_job = ProcessingJob(invoice_id=stale_invoice.id, state="queued", stage="queued", queued_at=now - timedelta(minutes=30))
    recent_job = ProcessingJob(invoice_id=recent_invoice.id, state="queued", stage="queued", queued_at=now)
    db.add_all([stale_job, recent_job])
    db.commit()
    stale_job_id, recent_job_id = stale_job.id, recent_job.id
    db.close()

    client = api["client"]
    listed = client.get("/api/v1/admin/processing-jobs/stale", headers=api["admin"])
    assert listed.status_code == 200, listed.text
    ids = {item["job_id"] for item in listed.json()["items"]}
    assert stale_job_id in ids
    assert recent_job_id not in ids

    recovered = client.post(
        f"/api/v1/admin/processing-jobs/{stale_job_id}/recover", headers=api["admin"],
        json={"expected_state": "queued", "stale_after_minutes": 10, "reason": "Queue worker was verified offline."},
    )
    assert recovered.status_code == 200, recovered.text
    too_early = client.post(
        f"/api/v1/admin/processing-jobs/{recent_job_id}/recover", headers=api["admin"],
        json={"expected_state": "queued", "stale_after_minutes": 5, "reason": "Premature recovery check."},
    )
    assert too_early.status_code == 409
    db = api["app"].state.session_factory()
    assert db.get(ProcessingJob, recent_job_id).state == "queued"
    assert db.get(ProcessingJob, stale_job_id).state == "failed"
    db.close()


def test_reprocess_dispatch_failure_leaves_job_queued_for_outbox_retry(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")

    def unavailable_queue(_job_id):
        raise ConnectionError("private broker details")

    monkeypatch.setattr("app.tasks.process_invoice_task.delay", unavailable_queue)
    db = api["app"].state.session_factory()
    invoice = Invoice(invoice_number="INV-REPROCESS-DISPATCH", source="test", processing_status="failed", workflow_status="review_required")
    db.add(invoice)
    db.commit()
    invoice_id = invoice.id
    db.close()

    response = api["client"].post(f"/api/v1/invoices/{invoice_id}/reprocess", headers=api["admin"])
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["state"] == "queued"
    assert result["stage"] == "queued"
    assert result["dispatch_state"] == "pending"
    assert result["dispatch_attempts"] == 1
    assert "private broker details" not in response.text
    detail = api["client"].get(f"/api/v1/invoices/{invoice_id}", headers=api["admin"])
    assert detail.status_code == 200
    assert detail.json()["job"]["state"] == "queued"
    assert detail.json()["job"]["dispatch_state"] == "pending"
    assert detail.json()["review_case"] is None


def test_overview_and_reconciliation_analytics_use_bounded_queries(api):
    db = api["app"].state.session_factory()
    for index in range(25):
        invoice = Invoice(
            invoice_number=f"INV-QUERY-{index}", source="test", currency="USD",
            processing_status="completed", workflow_status="ready_for_approval",
        )
        db.add(invoice)
        db.flush()
        db.add(InvoiceMatchResult(invoice_id=invoice.id, outcome="MATCHED", details={"financial_exposure": "0.00", "discrepancies": []}))
        db.add(FraudAssessment(invoice_id=invoice.id, heuristic_score=0.1, risk_band="low", is_probability=False, signals=[], model_versions={}))
    db.commit()
    db.close()

    statements = []
    engine = api["app"].state.engine
    def count_statement(*_args):
        statements.append(1)
    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        overview = api["client"].get("/api/v1/analytics/overview", headers=api["admin"])
        overview_queries = len(statements)
        statements.clear()
        reconciliation = api["client"].get("/api/v1/analytics/reconciliation", headers=api["admin"])
        reconciliation_queries = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert overview.status_code == 200
    assert overview.json()["total_invoices"] == 25
    assert reconciliation.status_code == 200
    assert reconciliation.json()["outcomes"] == [{"outcome": "MATCHED", "count": 25}]
    assert overview_queries < 15
    assert reconciliation_queries < 15


def test_invoice_csv_streams_rows_and_joins_latest_match_without_per_invoice_queries(api):
    db = api["app"].state.session_factory()
    invoice = Invoice(
        invoice_number="CSV-EXPORT-001", currency="EUR", total_amount=Decimal("12.34"),
        processing_status="completed", workflow_status="review_required",
        heuristic_risk_score=0.7, source="synthetic_test", is_synthetic=True,
    )
    db.add(invoice)
    db.add(Invoice(
        invoice_number="=1+1", currency="USD", total_amount=Decimal("1.00"),
        processing_status="completed", workflow_status="review_required",
        source="synthetic_test", is_synthetic=True,
    ))
    db.flush()
    db.add_all([
        InvoiceMatchResult(
            invoice_id=invoice.id, outcome="OLD_RESULT", details={},
            evaluated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        ),
        InvoiceMatchResult(
            invoice_id=invoice.id, outcome="LATEST_REVIEW", details={},
            evaluated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        ),
    ])
    db.commit()
    db.close()

    statements = []
    engine = api["app"].state.engine
    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        response = api["client"].get("/api/v1/reports/invoices.csv", headers=api["admin"])
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == "attachment; filename=invoice-register.csv"
    assert response.headers.get("x-request-id")
    rows = list(csv.reader(StringIO(response.text)))
    assert rows[0] == ["invoice_id", "invoice_number", "vendor", "issue_date", "currency", "total_amount", "workflow_status", "match_outcome", "heuristic_risk_score", "synthetic_demo_data"]
    exported = next(row for row in rows[1:] if row[1] == "CSV-EXPORT-001")
    assert exported[4:8] == ["EUR", "12.34", "review_required", "LATEST_REVIEW"]
    assert exported[9] == "True"
    assert any(row[1] == "'=1+1" for row in rows[1:])
    invoice_selects = [statement for statement in statements if "invoices" in statement]
    assert len(invoice_selects) == 1, statements


def test_invoice_and_review_list_payloads_use_batched_related_queries(api):
    db = api["app"].state.session_factory()
    vendor = Vendor(code="BATCH-SEARCH", name="Northstar Search Vendor", aliases=[], risk_tier="standard")
    db.add(vendor)
    db.flush()
    for index in range(25):
        invoice = Invoice(
            invoice_number=f"INV-BATCH-{index:03d}", vendor_id=vendor.id if index == 0 else None,
            currency="USD", total_amount=Decimal("10.00"),
            processing_status="completed", workflow_status="review_required", source="synthetic_test",
        )
        db.add(invoice)
        db.flush()
        db.add_all([
            DocumentFile(
                invoice_id=invoice.id, original_filename=f"synthetic-{index}.pdf",
                stored_path=f"/synthetic/{invoice.id}.pdf", mime_type="application/pdf",
                sha256=f"{index:064x}", byte_size=128,
            ),
            ProcessingJob(invoice_id=invoice.id, state="completed", stage="complete"),
            InvoiceMatchResult(invoice_id=invoice.id, outcome="MANUAL_REVIEW", details={"financial_exposure": "1.00"}),
            FraudAssessment(invoice_id=invoice.id, heuristic_score=0.5, risk_band="medium", is_probability=False, signals=[], model_versions={}),
            ReviewCase(invoice_id=invoice.id, status="open", priority="normal", reason_codes=["synthetic_test"], summary="Synthetic test case"),
            FraudAlert(invoice_id=invoice.id, alert_type="synthetic_test", severity=0.5, title="Synthetic alert", explanation="Test evidence", evidence={}),
        ])
    db.commit()
    db.close()

    engine = api["app"].state.engine
    def query_count(path: str):
        statements = []
        def record_statement(*_args):
            statements.append(1)
        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            response = api["client"].get(path, headers=api["admin"])
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)
        return response, len(statements)

    invoice_response, invoice_queries = query_count("/api/v1/invoices?limit=100")
    review_response, review_queries = query_count("/api/v1/review/cases?limit=100")
    assert invoice_response.status_code == 200, invoice_response.text
    assert invoice_response.json()["total"] == 25
    assert invoice_response.json()["items"][0]["document"]["filename"].startswith("synthetic-")
    assert invoice_response.json()["items"][0]["job"]["state"] == "completed"
    assert invoice_response.json()["items"][0]["open_alert_count"] == 1
    assert review_response.status_code == 200, review_response.text
    assert review_response.json()["total"] == 25
    assert review_response.json()["items"][0]["invoice"]["match_outcome"] == "MANUAL_REVIEW"
    assert review_response.json()["items"][0]["invoice"]["risk_band"] == "medium"
    vendor_search = api["client"].get("/api/v1/invoices?limit=100&search=Northstar%20Search", headers=api["admin"])
    assert vendor_search.status_code == 200
    assert vendor_search.json()["total"] == 1
    assert vendor_search.json()["items"][0]["vendor_name"] == "Northstar Search Vendor"
    assert invoice_queries < 20
    assert review_queries < 25


def test_human_line_correction_preserves_source_box_without_claiming_ocr_confidence(api):
    client = api["client"]
    with api["app"].state.session_factory() as db:
        invoice = Invoice(invoice_number="LINE-CORRECTION-TEST", currency="USD", total_amount=Decimal("10.00"), workflow_status="review_required")
        db.add(invoice)
        db.flush()
        invoice_id = invoice.id
        existing_versions = db.query(ExtractionResult).filter_by(invoice_id=invoice_id).count()
        db.add(ExtractionResult(
            invoice_id=invoice_id, raw_text="Widget XL 2 5.00 10.00",
            extracted_fields={"line_items": [{
                "line_number": 1, "sku": "SKU-1", "description": "Widget XL", "quantity": "2",
                "unit_price": "5.00", "tax_rate": "0", "line_total": "10.00", "confidence": 0.82,
                "source_page": 1, "bbox": [20.0, 40.0, 240.0, 60.0],
                "validation_status": "arithmetic_valid", "confidence_basis": "synthetic source evidence",
            }]},
            tokens=[], validation_errors=[], engine_name="synthetic-test", engine_version="test-v1",
            extraction_version=existing_versions + 1,
        ))
        db.commit()

    response = client.patch(
        f"/api/v1/invoices/{invoice_id}/extraction", headers=api["admin"],
        json={"reason": "Corrected description after human source review", "values": {"line_items": [{
            "sku": "SKU-1", "description": "Widget XL corrected", "quantity": "2",
            "unit_price": "5.00", "line_total": "10.00", "tax_rate": "0",
        }]}},
    )
    assert response.status_code == 200, response.text
    detail = client.get(f"/api/v1/invoices/{invoice_id}", headers=api["admin"])
    assert detail.status_code == 200, detail.text
    corrected = detail.json()["extraction"]["fields"]["line_items"][0]
    assert corrected["description"] == "Widget XL corrected"
    assert corrected["source_page"] == 1
    assert corrected["bbox"] == [20.0, 40.0, 240.0, 60.0]
    assert corrected["confidence"] is None
    assert corrected["validation_status"] == "human_corrected"
    assert "Human-entered" in corrected["confidence_basis"]
    with api["app"].state.session_factory() as db:
        assert db.query(ModelFeedback).filter_by(invoice_id=invoice_id).count() >= 1


def test_correction_revalidates_required_fields_instead_of_silently_clearing_them(api):
    with api["app"].state.session_factory() as db:
        invoice = Invoice(
            invoice_number="REQUIRED-FIELD-CORRECTION-TEST", total_amount=Decimal("10.00"),
            issue_date=date(2026, 10, 1), workflow_status="review_required",
        )
        db.add(invoice)
        db.flush()
        invoice_id = invoice.id
        db.add(ExtractionResult(
            invoice_id=invoice_id, raw_text="Synthetic invoice", extracted_fields={
                "vendor_name": {"value": "Synthetic Vendor", "normalized_value": "Synthetic Vendor"},
                "invoice_number": {"value": "REQUIRED-FIELD-CORRECTION-TEST", "normalized_value": "REQUIRED-FIELD-CORRECTION-TEST"},
                "issue_date": {"value": "2026-10-01", "normalized_value": "2026-10-01"},
                "total_amount": {"value": "10.00", "normalized_value": "10.00"},
            },
            tokens=[], validation_errors=[], engine_name="synthetic-test", engine_version="test-v1",
            extraction_version=1,
        ))
        db.commit()

    response = api["client"].patch(
        f"/api/v1/invoices/{invoice_id}/extraction", headers=api["admin"],
        json={"reason": "Synthetic required-field revalidation regression", "values": {"invoice_number": "   "}},
    )
    assert response.status_code == 200, response.text
    with api["app"].state.session_factory() as db:
        corrected = db.query(ExtractionResult).filter_by(invoice_id=invoice_id).order_by(ExtractionResult.extraction_version.desc()).first()
        assert corrected.extracted_fields["invoice_number"]["normalized_value"] == ""
        assert any(error.get("code") == "missing_invoice_number" for error in corrected.validation_errors)


def test_prior_po_invoices_use_latest_match_and_ignore_rejected_documents(api):
    with api["app"].state.session_factory() as db:
        vendor = Vendor(code="CUMULATIVE-PO-TEST", name="Cumulative Test Vendor", aliases=[])
        db.add(vendor)
        db.flush()
        po = PurchaseOrder(number="PO-CUMULATIVE-TEST", vendor_id=vendor.id, issue_date=date(2026, 1, 1))
        db.add(po)
        db.flush()
        prior = Invoice(
            invoice_number="PRIOR-PO-INVOICE", purchase_order_id=po.id,
            workflow_status="ready_for_approval", total_amount=Decimal("100.00"),
        )
        rejected = Invoice(
            invoice_number="REJECTED-PO-INVOICE", purchase_order_id=po.id,
            workflow_status="rejected_by_reviewer", total_amount=Decimal("500.00"),
        )
        current = Invoice(
            invoice_number="CURRENT-PO-INVOICE", purchase_order_id=po.id,
            workflow_status="processing", total_amount=Decimal("50.00"),
        )
        db.add_all([prior, rejected, current])
        db.flush()
        db.add_all([
            InvoiceMatchResult(invoice_id=prior.id, outcome="MATCHED", evaluated_at=datetime(2026, 2, 1, tzinfo=timezone.utc), details={"line_results": [{"po_line_number": 1, "invoice_quantity": "1"}]}),
            InvoiceMatchResult(invoice_id=prior.id, outcome="MISMATCH", evaluated_at=datetime(2026, 3, 1, tzinfo=timezone.utc), details={"line_results": [{"po_line_number": 1, "invoice_quantity": "2"}]}),
            InvoiceMatchResult(invoice_id=rejected.id, outcome="MATCHED", details={"line_results": [{"po_line_number": 1, "invoice_quantity": "10"}]}),
            InvoiceMatchResult(invoice_id=current.id, outcome="MATCHED", details={"line_results": [{"po_line_number": 1, "invoice_quantity": "99"}]}),
        ])
        db.commit()
        prior_invoice_id, current_invoice_id, po_id = prior.id, current.id, po.id

    with api["app"].state.session_factory() as db:
        quantities = _prior_invoice_quantities(db, po_id, current_invoice_id)
    assert quantities == {"1": Decimal("2")}
