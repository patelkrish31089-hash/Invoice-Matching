from __future__ import annotations

import json

from sqlalchemy import select

from app.models import AuditEvent, Invoice, ModelFeedback, ReviewCase, User


def test_admin_export_contains_only_approved_correction_pairs_and_omits_reviewer_notes(api):
    with api["app"].state.session_factory() as db:
        admin = db.scalar(select(User).where(User.email == "admin@test.local"))
        reviewer = db.scalar(select(User).where(User.email == "reviewer@test.local"))
        approved_invoice = Invoice(invoice_number="FEEDBACK-APPROVED", source="upload", is_synthetic=True)
        pending_invoice = Invoice(invoice_number="FEEDBACK-PENDING", source="upload", is_synthetic=True)
        db.add_all([approved_invoice, pending_invoice])
        db.flush()
        resolved_case = ReviewCase(
            invoice_id=approved_invoice.id, status="approved", priority="normal",
            reason_codes=["human_correction"], summary="Resolved synthetic review",
        )
        db.add(resolved_case)
        db.add_all([
            ModelFeedback(
                invoice_id=approved_invoice.id, reviewer_id=reviewer.id,
                correction_payload={"previous": {"invoice_number": "INV-O", "total_amount": "10.00"}, "corrected": {"invoice_number": "INV-0", "total_amount": "10.00"}},
                reason="PRIVATE_REVIEW_NOTE_DO_NOT_EXPORT", approved_for_training=True,
            ),
            ModelFeedback(
                invoice_id=pending_invoice.id, reviewer_id=reviewer.id,
                correction_payload={"previous": {"total_amount": "1.00"}, "corrected": {"total_amount": "100.00"}},
                reason="Unapproved example", approved_for_training=False,
            ),
        ])
        db.commit()

    response = api["client"].get("/api/v1/review/feedback/training-export", headers=api["admin"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["x-feedback-export-total"] == "1"
    rows = [json.loads(line) for line in response.text.splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["label"] == {"invoice_number": "INV-0", "total_amount": "10.00"}
    assert rows[0]["prediction"]["invoice_number"] == "INV-O"
    assert "PRIVATE_REVIEW_NOTE_DO_NOT_EXPORT" not in response.text
    assert "FEEDBACK-PENDING" not in response.text

    with api["app"].state.session_factory() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "ml.approved_feedback_exported"))
        assert event is not None
        assert event.details == {"returned": 1, "approved_total": 1, "skipped_invalid_or_oversize": 0, "limit": 500, "offset": 0}


def test_feedback_export_is_admin_only(api):
    response = api["client"].get("/api/v1/review/feedback/training-export", headers=api["auditor"])
    assert response.status_code == 403
