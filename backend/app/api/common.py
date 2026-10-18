from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditEvent, DocumentFile, ExtractionResult, FraudAlert, FraudAssessment, Invoice,
    InvoiceMatchResult, ProcessingJob, ProcessingOutbox, PurchaseOrder, ReviewCase, ReviewDecision,
    User, Vendor,
)


def user_payload(user) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "roles": sorted(role.name for role in user.roles),
        "vendor_id": user.vendor_id,
    }


def latest_extraction(db: Session, invoice_id: str) -> ExtractionResult | None:
    return db.scalar(select(ExtractionResult).where(ExtractionResult.invoice_id == invoice_id).order_by(ExtractionResult.extraction_version.desc()).limit(1))


def latest_match(db: Session, invoice_id: str) -> InvoiceMatchResult | None:
    return db.scalar(select(InvoiceMatchResult).where(InvoiceMatchResult.invoice_id == invoice_id).order_by(InvoiceMatchResult.evaluated_at.desc()).limit(1))


def latest_assessment(db: Session, invoice_id: str) -> FraudAssessment | None:
    return db.scalar(select(FraudAssessment).where(FraudAssessment.invoice_id == invoice_id).order_by(FraudAssessment.analyzed_at.desc()).limit(1))


def invoice_summary(
    db: Session,
    invoice: Invoice,
    *,
    include_detail: bool = False,
    _batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _batch is not None:
        documents = _batch["documents"].get(invoice.id, [])
        latest_job = _batch["jobs"].get(invoice.id)
        latest = _batch["matches"].get(invoice.id)
        assessment = _batch["assessments"].get(invoice.id)
        case = _batch["cases"].get(invoice.id)
        alert_count = _batch["alert_counts"].get(invoice.id, 0)
        vendor_name = _batch["vendor_names"].get(invoice.vendor_id)
        po_number = _batch["po_numbers"].get(invoice.purchase_order_id)
        alerts = []
    else:
        documents = invoice.documents
        latest_job = db.scalar(select(ProcessingJob).where(ProcessingJob.invoice_id == invoice.id).order_by(ProcessingJob.queued_at.desc(), ProcessingJob.id.desc()).limit(1))
        latest = latest_match(db, invoice.id)
        assessment = latest_assessment(db, invoice.id)
        case = db.scalar(select(ReviewCase).where(ReviewCase.invoice_id == invoice.id, ReviewCase.status.in_(["open", "in_review", "waiting_documents"])).order_by(ReviewCase.created_at.desc(), ReviewCase.id.desc()).limit(1))
        vendor_name = invoice.vendor.name if invoice.vendor else None
        po_number = invoice.purchase_order.number if invoice.purchase_order else None
        if include_detail:
            alerts = db.scalars(select(FraudAlert).where(FraudAlert.invoice_id == invoice.id, FraudAlert.status == "open").order_by(FraudAlert.created_at.desc(), FraudAlert.id.desc())).all()
            alert_count = len(alerts)
        else:
            alerts = []
            alert_count = db.scalar(select(func.count()).select_from(FraudAlert).where(FraudAlert.invoice_id == invoice.id, FraudAlert.status == "open")) or 0
    if include_detail and _batch is not None:
        raise ValueError("Batch invoice summaries are limited to list views")

    result: dict[str, Any] = {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "vendor_id": invoice.vendor_id,
        "vendor_name": vendor_name,
        "currency": invoice.currency,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "subtotal": str(invoice.subtotal) if invoice.subtotal is not None else None,
        "tax_amount": str(invoice.tax_amount) if invoice.tax_amount is not None else None,
        "shipping_amount": str(invoice.shipping_amount),
        "discount_amount": str(invoice.discount_amount),
        "total_amount": str(invoice.total_amount) if invoice.total_amount is not None else None,
        "processing_status": invoice.processing_status,
        "workflow_status": invoice.workflow_status,
        "extraction_confidence": invoice.extraction_confidence,
        "heuristic_risk_score": invoice.heuristic_risk_score,
        "risk_band": assessment.risk_band if assessment else None,
        "match_outcome": latest.outcome if latest else None,
        "purchase_order_id": invoice.purchase_order_id,
        "purchase_order_number": po_number,
        "source": invoice.source,
        "is_synthetic": invoice.is_synthetic,
        "created_at": invoice.created_at.isoformat() if invoice.created_at else None,
        "updated_at": invoice.updated_at.isoformat() if invoice.updated_at else None,
        "document": ({
            "id": documents[0].id, "filename": documents[0].original_filename,
            "mime_type": documents[0].mime_type, "sha256": documents[0].sha256,
            "byte_size": documents[0].byte_size, "page_count": documents[0].page_count,
        } if documents else None),
        "job": ({
            "id": latest_job.id, "state": latest_job.state, "stage": latest_job.stage,
            "attempts": latest_job.attempts, "error_message": latest_job.error_message,
            "queued_at": latest_job.queued_at.isoformat() if latest_job.queued_at else None,
            "started_at": latest_job.started_at.isoformat() if latest_job.started_at else None,
            "heartbeat_at": latest_job.heartbeat_at.isoformat() if latest_job.heartbeat_at else None,
            "finished_at": latest_job.finished_at.isoformat() if latest_job.finished_at else None,
        } if latest_job else None),
        "review_case": ({"id": case.id, "status": case.status, "priority": case.priority, "reason_codes": case.reason_codes} if case else None),
        "open_alert_count": alert_count,
    }
    if include_detail:
        if latest_job and result.get("job"):
            dispatch_event = db.scalar(
                select(ProcessingOutbox)
                .where(ProcessingOutbox.job_id == latest_job.id, ProcessingOutbox.event_type == "process_invoice")
                .limit(1)
            )
            if dispatch_event:
                result["job"].update({
                    "dispatch_state": dispatch_event.state,
                    "dispatch_attempts": dispatch_event.attempts,
                    "dispatch_next_attempt_at": dispatch_event.available_at.isoformat() if dispatch_event.available_at else None,
                    "dispatch_error_type": dispatch_event.last_error_type,
                })
        extraction = latest_extraction(db, invoice.id)
        result["lines"] = [{
            "id": line.id, "line_number": line.line_number, "sku": line.sku,
            "description": line.description, "quantity": str(line.quantity),
            "unit_price": str(line.unit_price), "tax_rate": str(line.tax_rate),
            "tax_amount": str(line.tax_amount) if line.tax_amount is not None else None,
            "line_total": str(line.line_total),
        } for line in invoice.lines]
        result["extraction"] = ({
            "id": extraction.id, "raw_text": extraction.raw_text,
            "fields": extraction.extracted_fields, "tokens": extraction.tokens,
            "validation_errors": extraction.validation_errors,
            "engine_name": extraction.engine_name, "engine_version": extraction.engine_version,
            "version": extraction.extraction_version,
            "extracted_at": extraction.extracted_at.isoformat() if extraction.extracted_at else None,
        } if extraction else None)
        result["matching"] = latest.details if latest else None
        result["fraud"] = ({
            "heuristic_score": assessment.heuristic_score, "risk_band": assessment.risk_band,
            "is_probability": assessment.is_probability, "signals": assessment.signals,
            "model_versions": assessment.model_versions,
            "analyzed_at": assessment.analyzed_at.isoformat() if assessment.analyzed_at else None,
        } if assessment else None)
        result["alerts"] = [{
            "id": alert.id, "type": alert.alert_type, "severity": alert.severity,
            "title": alert.title, "explanation": alert.explanation,
            "evidence": alert.evidence, "status": alert.status,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
        } for alert in alerts]
        result["review_history"] = _review_history(db, case.id) if case else []
    return result


def invoice_summaries(db: Session, invoices: list[Invoice]) -> list[dict[str, Any]]:
    """Serialize a page of invoices with a bounded set of batched related-data queries."""
    if not invoices:
        return []
    invoice_ids = [invoice.id for invoice in invoices]
    vendor_ids = {invoice.vendor_id for invoice in invoices if invoice.vendor_id}
    po_ids = {invoice.purchase_order_id for invoice in invoices if invoice.purchase_order_id}

    documents_by_invoice: dict[str, list[DocumentFile]] = defaultdict(list)
    for document in db.scalars(select(DocumentFile).where(DocumentFile.invoice_id.in_(invoice_ids)).order_by(DocumentFile.invoice_id, DocumentFile.id)).all():
        documents_by_invoice[document.invoice_id].append(document)
    vendor_names = dict(db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(vendor_ids))).all()) if vendor_ids else {}
    po_numbers = dict(db.execute(select(PurchaseOrder.id, PurchaseOrder.number).where(PurchaseOrder.id.in_(po_ids))).all()) if po_ids else {}

    ranked_jobs = select(
        ProcessingJob.invoice_id.label("invoice_id"), ProcessingJob.id.label("id"),
        ProcessingJob.state.label("state"), ProcessingJob.stage.label("stage"),
        ProcessingJob.attempts.label("attempts"), ProcessingJob.error_message.label("error_message"),
        ProcessingJob.queued_at.label("queued_at"), ProcessingJob.started_at.label("started_at"),
        ProcessingJob.heartbeat_at.label("heartbeat_at"), ProcessingJob.finished_at.label("finished_at"),
        func.row_number().over(
            partition_by=ProcessingJob.invoice_id,
            order_by=(ProcessingJob.queued_at.desc(), ProcessingJob.id.desc()),
        ).label("row_rank"),
    ).where(ProcessingJob.invoice_id.in_(invoice_ids)).subquery()
    jobs = {row.invoice_id: row for row in db.execute(select(ranked_jobs).where(ranked_jobs.c.row_rank == 1)).all()}

    ranked_matches = select(
        InvoiceMatchResult.invoice_id.label("invoice_id"), InvoiceMatchResult.id.label("id"),
        InvoiceMatchResult.outcome.label("outcome"), InvoiceMatchResult.details.label("details"),
        func.row_number().over(
            partition_by=InvoiceMatchResult.invoice_id,
            order_by=(InvoiceMatchResult.evaluated_at.desc(), InvoiceMatchResult.id.desc()),
        ).label("row_rank"),
    ).where(InvoiceMatchResult.invoice_id.in_(invoice_ids)).subquery()
    matches = {row.invoice_id: row for row in db.execute(select(ranked_matches).where(ranked_matches.c.row_rank == 1)).all()}

    ranked_assessments = select(
        FraudAssessment.invoice_id.label("invoice_id"), FraudAssessment.id.label("id"),
        FraudAssessment.risk_band.label("risk_band"),
        func.row_number().over(
            partition_by=FraudAssessment.invoice_id,
            order_by=(FraudAssessment.analyzed_at.desc(), FraudAssessment.id.desc()),
        ).label("row_rank"),
    ).where(FraudAssessment.invoice_id.in_(invoice_ids)).subquery()
    assessments = {row.invoice_id: row for row in db.execute(select(ranked_assessments).where(ranked_assessments.c.row_rank == 1)).all()}

    ranked_cases = select(
        ReviewCase.invoice_id.label("invoice_id"), ReviewCase.id.label("id"),
        ReviewCase.status.label("status"), ReviewCase.priority.label("priority"),
        ReviewCase.reason_codes.label("reason_codes"),
        func.row_number().over(
            partition_by=ReviewCase.invoice_id,
            order_by=(ReviewCase.created_at.desc(), ReviewCase.id.desc()),
        ).label("row_rank"),
    ).where(
        ReviewCase.invoice_id.in_(invoice_ids),
        ReviewCase.status.in_(["open", "in_review", "waiting_documents"]),
    ).subquery()
    cases = {row.invoice_id: row for row in db.execute(select(ranked_cases).where(ranked_cases.c.row_rank == 1)).all()}

    alert_counts = dict(db.execute(
        select(FraudAlert.invoice_id, func.count(FraudAlert.id))
        .where(FraudAlert.invoice_id.in_(invoice_ids), FraudAlert.status == "open")
        .group_by(FraudAlert.invoice_id)
    ).all())
    batch = {
        "documents": documents_by_invoice, "vendor_names": vendor_names, "po_numbers": po_numbers,
        "jobs": jobs, "matches": matches, "assessments": assessments,
        "cases": cases, "alert_counts": alert_counts,
    }
    return [invoice_summary(db, invoice, _batch=batch) for invoice in invoices]


def _review_history_payload(decisions: list[ReviewDecision]) -> list[dict[str, Any]]:
    return [{
        "id": item.id, "decision": item.decision, "reason": item.reason,
        "reviewer_id": item.reviewer_id, "created_at": item.created_at.isoformat(),
        "evidence": item.evidence,
    } for item in decisions]


def _review_history(db: Session, case_id: str) -> list[dict[str, Any]]:
    decisions = db.scalars(select(ReviewDecision).where(ReviewDecision.case_id == case_id).order_by(ReviewDecision.created_at.asc())).all()
    return _review_history_payload(decisions)


def case_payload(db: Session, case: ReviewCase, *, include_detail: bool = False) -> dict[str, Any]:
    payload = {
        "id": case.id, "invoice_id": case.invoice_id, "status": case.status,
        "priority": case.priority, "reason_codes": case.reason_codes,
        "summary": case.summary, "assigned_to_id": case.assigned_to_id,
        "assigned_to": case.assigned_to.display_name if case.assigned_to else None,
        "created_at": case.created_at.isoformat(), "updated_at": case.updated_at.isoformat(),
        "invoice": invoice_summary(db, case.invoice),
        "history": _review_history(db, case.id),
    }
    if include_detail:
        payload["invoice"] = invoice_summary(db, case.invoice, include_detail=True)
        payload["related_audit"] = [
            {"sequence": event.sequence, "action": event.action, "details": event.details, "occurred_at": event.occurred_at.isoformat()}
            for event in db.scalars(select(AuditEvent).where(AuditEvent.object_id == case.invoice_id).order_by(AuditEvent.sequence.asc())).all()
        ]
    return payload


def case_payloads(db: Session, cases: list[ReviewCase]) -> list[dict[str, Any]]:
    """Build a paginated review queue without per-case invoice/history lookups."""
    if not cases:
        return []
    case_ids = [case.id for case in cases]
    invoice_ids = {case.invoice_id for case in cases}
    assigned_ids = {case.assigned_to_id for case in cases if case.assigned_to_id}
    invoices = db.scalars(select(Invoice).where(Invoice.id.in_(invoice_ids))).all()
    invoice_by_id = {invoice.id: invoice for invoice in invoices}
    summaries = {item["id"]: item for item in invoice_summaries(db, invoices)}
    assignee_names = dict(db.execute(select(User.id, User.display_name).where(User.id.in_(assigned_ids))).all()) if assigned_ids else {}
    history_by_case: dict[str, list[ReviewDecision]] = defaultdict(list)
    decisions = db.scalars(
        select(ReviewDecision).where(ReviewDecision.case_id.in_(case_ids)).order_by(ReviewDecision.created_at.asc(), ReviewDecision.id.asc())
    ).all()
    for decision in decisions:
        history_by_case[decision.case_id].append(decision)
    return [{
        "id": case.id, "invoice_id": case.invoice_id, "status": case.status,
        "priority": case.priority, "reason_codes": case.reason_codes,
        "summary": case.summary, "assigned_to_id": case.assigned_to_id,
        "assigned_to": assignee_names.get(case.assigned_to_id),
        "created_at": case.created_at.isoformat(), "updated_at": case.updated_at.isoformat(),
        "invoice": summaries.get(case.invoice_id),
        "history": _review_history_payload(history_by_case[case.id]),
    } for case in cases]
