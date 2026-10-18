from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
import hashlib
import json
import logging
import re

import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.config import PROJECT_ROOT, settings
from app.models import (
    ApplicationSetting, DocumentFile, Employee, ExtractionResult, FraudAlert,
    FraudAssessment, GoodsReceipt, GoodsReceiptLine, Invoice, InvoiceLine,
    InvoiceMatchResult, ModelVersion, ProcessingJob, PurchaseOrder,
    PurchaseOrderLine, ReviewCase, Vendor,
)
from app.services.document_processing import OCRUnavailable, process_document
from app.services.fraud import (
    aggregate_heuristic_risk, benford_analysis, build_behavior_vector,
    detect_duplicates, relationship_overlaps, score_isolation_forest,
    train_isolation_forest, vendor_behavior_features,
)
from app.services.matching import D, TolerancePolicy, reconcile

logger = logging.getLogger("ledgerlens.processing")


def _field_value(fields: dict[str, Any], name: str) -> Any:
    return (fields.get(name) or {}).get("normalized_value")


def _field_confidence(fields: dict[str, Any], name: str) -> float:
    return float((fields.get(name) or {}).get("confidence") or 0.0)


def _resolve_vendor(db: Session, fields: dict[str, Any]) -> Vendor | None:
    tax_id = _field_value(fields, "vendor_tax_id")
    if tax_id:
        normalized_tax = re.sub(r"\W", "", str(tax_id)).lower()
        for vendor in db.scalars(select(Vendor).where(Vendor.is_active.is_(True))):
            if vendor.tax_identifier and re.sub(r"\W", "", vendor.tax_identifier).lower() == normalized_tax:
                return vendor
    name = str(_field_value(fields, "vendor_name") or "").strip()
    if not name:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    candidates = []
    for vendor in db.scalars(select(Vendor).where(Vendor.is_active.is_(True))):
        options = [vendor.name, *(vendor.aliases or [])]
        score = max(float(fuzz.ratio(normalized, re.sub(r"[^a-z0-9]+", " ", item.lower()).strip())) for item in options)
        candidates.append((score, vendor))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if candidates and candidates[0][0] >= 92 and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 5):
        return candidates[0][1]
    return None


def _vendor_identity_review_reason(
    bound_vendor: Vendor, fields: dict[str, Any], resolved_vendor: Vendor | None,
) -> str | None:
    """Require a configured exact supplier identity cross-check for vendor submissions."""
    if not bound_vendor.is_active:
        return "vendor_account_inactive"
    extracted_tax_id = _field_value(fields, "vendor_tax_id")
    if bound_vendor.tax_identifier:
        if not extracted_tax_id:
            return "vendor_identity_unverified"
        normalize = lambda value: re.sub(r"\W", "", str(value)).casefold()
        if normalize(extracted_tax_id) != normalize(bound_vendor.tax_identifier):
            return "vendor_identity_mismatch"
    elif extracted_tax_id:
        # A document tax identifier cannot be verified if the reference record has none.
        return "vendor_identity_unverified"

    extracted_name = str(_field_value(fields, "vendor_name") or "").strip()
    if not extracted_name:
        return "vendor_identity_unverified"
    normalize_name = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
    expected_names = {normalize_name(bound_vendor.name), *(normalize_name(item) for item in (bound_vendor.aliases or []))}
    if normalize_name(extracted_name) not in expected_names:
        if resolved_vendor and resolved_vendor.id != bound_vendor.id:
            return "vendor_identity_mismatch"
        return "vendor_identity_unverified"
    if resolved_vendor and resolved_vendor.id != bound_vendor.id:
        return "vendor_identity_mismatch"
    return None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _po_payload(po: PurchaseOrder | None) -> dict[str, Any] | None:
    if po is None:
        return None
    return {
        "id": po.id, "number": po.number, "vendor_id": po.vendor_id,
        "currency": po.currency, "status": po.status,
        "issue_date": po.issue_date.isoformat(), "expected_total": str(po.expected_total),
        "lines": [{
            "id": line.id, "line_number": line.line_number, "sku": line.sku,
            "description": line.description, "quantity": str(line.quantity),
            "unit_price": str(line.unit_price), "tax_rate": str(line.tax_rate),
        } for line in po.lines],
    }


def _receipt_payload(receipts: list[GoodsReceipt]) -> list[dict[str, Any]]:
    return [{
        "id": receipt.id, "number": receipt.number,
        "received_date": receipt.received_date.isoformat(), "status": receipt.status,
        "lines": [{
            "id": line.id, "purchase_order_line_id": line.purchase_order_line_id,
            "sku": line.sku, "description": line.description, "quantity": str(line.quantity),
        } for line in receipt.lines],
    } for receipt in receipts]


def _prior_invoice_quantities(db: Session, purchase_order_id: str, exclude_invoice_id: str) -> dict[str, Decimal]:
    """Sum the latest stored line mappings for non-rejected invoices already using this PO."""
    rows = db.execute(
        select(InvoiceMatchResult, Invoice.workflow_status)
        .join(Invoice, Invoice.id == InvoiceMatchResult.invoice_id)
        .where(Invoice.purchase_order_id == purchase_order_id, Invoice.id != exclude_invoice_id)
        .order_by(InvoiceMatchResult.evaluated_at.desc(), InvoiceMatchResult.id.desc())
    ).all()
    seen_invoice_ids: set[str] = set()
    totals: dict[str, Decimal] = {}
    excluded_states = {"rejected", "rejected_by_reviewer", "cancelled", "canceled", "void", "voided"}
    for match_result, workflow_status in rows:
        if match_result.invoice_id in seen_invoice_ids:
            continue
        seen_invoice_ids.add(match_result.invoice_id)
        if str(workflow_status or "").casefold() in excluded_states:
            continue
        for line in (match_result.details or {}).get("line_results", []):
            line_number = line.get("po_line_number")
            quantity = line.get("invoice_quantity")
            if line_number is None or quantity is None:
                continue
            try:
                key = str(int(line_number))
                value = Decimal(str(quantity))
                if value > 0:
                    totals[key] = totals.get(key, Decimal("0")) + value
            except (ValueError, TypeError, ArithmeticError):
                continue
    return totals


def _history_payload(db: Session, exclude_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(Invoice).where(Invoice.id != exclude_id, Invoice.total_amount.is_not(None))).all()
    result = []
    for row in rows:
        doc = row.documents[0] if row.documents else None
        result.append({
            "id": row.id, "invoice_number": row.invoice_number,
            "vendor_id": row.vendor_id, "vendor_name": row.vendor.name if row.vendor else None,
            "issue_date": row.issue_date.isoformat() if row.issue_date else None,
            "total_amount": str(row.total_amount) if row.total_amount is not None else None,
            "document_hash": doc.sha256 if doc else None,
            "line_items": [{
                "sku": line.sku, "description": line.description,
                "quantity": str(line.quantity), "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
            } for line in row.lines],
        })
    return result


def _history_before_issue_date(history_rows: list[Invoice], issue_date: date | None) -> list[Invoice]:
    """Keep future-issued invoices out of a point-in-time anomaly reference cohort."""
    if issue_date is None:
        return []
    return [row for row in history_rows if row.issue_date is not None and row.issue_date < issue_date]


def _history_behavior_matrix(db: Session, history_rows: list[Invoice]) -> list[list[float]]:
    ordered = sorted([row for row in history_rows if row.issue_date and row.total_amount is not None], key=lambda row: (row.issue_date, row.created_at))
    prior: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    for row in ordered:
        row_data = {
            "id": row.id, "vendor_id": row.vendor_id,
            "issue_date": row.issue_date.isoformat(), "total_amount": str(row.total_amount),
        }
        behavior = vendor_behavior_features(row_data, prior)
        amount_ratio = behavior["amount_to_median_ratio"] or 1.0
        count7 = behavior["invoices_in_prior_7_days"]
        days_previous = behavior["days_since_previous_invoice"] or 365
        vectors.append(build_behavior_vector(float(row.total_amount), amount_ratio, count7, days_previous, 0.0, 0.0))
        prior.append(row_data)
    return vectors


def _run_fraud_analysis(db: Session, invoice: Invoice, match_details: dict[str, Any], document_hash: str | None, benford_min_sample: int | None = None) -> tuple[float, str, list[dict[str, Any]], dict[str, str]]:
    current = {
        "id": invoice.id, "invoice_number": invoice.invoice_number,
        "vendor_id": invoice.vendor_id, "vendor_name": invoice.vendor.name if invoice.vendor else None,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "total_amount": str(invoice.total_amount) if invoice.total_amount is not None else None,
        "document_hash": document_hash,
        "line_items": [{
            "sku": line.sku, "description": line.description,
            "quantity": str(line.quantity), "unit_price": str(line.unit_price),
            "line_total": str(line.line_total),
        } for line in invoice.lines],
    }
    history = _history_payload(db, invoice.id)
    signals: list[dict[str, Any]] = []
    model_versions = {"duplicate_rules": "duplicate-v1", "risk_aggregation": "heuristic-index-v1"}

    for signal in detect_duplicates(current, history):
        signals.append({
            **signal, "title": "Possible duplicate invoice",
            "weight": 1.0, "estimated_exposure": str(invoice.total_amount or Decimal("0")),
        })

    for discrepancy in match_details.get("discrepancies", []):
        anomaly_types = {"unit_price_variance", "quantity_above_received", "quantity_above_ordered", "line_arithmetic"}
        if discrepancy.get("type") in anomaly_types:
            severity = 0.95 if discrepancy["type"] in {"unit_price_variance", "quantity_above_received", "quantity_above_ordered"} else 0.55
            signals.append({
                "type": discrepancy["type"], "severity": severity, "weight": 1.0,
                "title": discrepancy["type"].replace("_", " ").title(),
                "explanation": f"{discrepancy.get('rule')}: expected {discrepancy.get('expected')}, observed {discrepancy.get('actual')}.",
                "evidence": discrepancy,
                "estimated_exposure": match_details.get("financial_exposure", "0.00"),
            })
    if match_details.get("outcome") in {"MISSING_PO", "MISSING_GRN", "MISMATCH", "MANUAL_REVIEW"}:
        signals.append({
            "type": "reconciliation_exception", "severity": 0.28, "weight": 0.5,
            "title": "Reconciliation exception", "explanation": match_details.get("explanation", "Reconciliation requires review."),
            "evidence": {"outcome": match_details.get("outcome"), "discrepancy_count": len(match_details.get("discrepancies", []))},
            "estimated_exposure": match_details.get("financial_exposure", "0.00"),
        })

    history_rows = db.scalars(select(Invoice).where(Invoice.id != invoice.id, Invoice.total_amount.is_not(None))).all()
    prior_history_rows = _history_before_issue_date(history_rows, invoice.issue_date)
    behavior = vendor_behavior_features(current, history)
    if behavior["indicators"]:
        signals.append({
            "type": "vendor_behavior_anomaly", "severity": 0.62, "weight": 0.75,
            "title": "Vendor billing pattern changed",
            "explanation": "Rolling vendor history crossed one or more configured screening rules: " + ", ".join(behavior["indicators"]) + ".",
            "evidence": behavior, "estimated_exposure": str(invoice.total_amount or Decimal("0")),
        })

    vendor_amounts = [float(row.total_amount) for row in prior_history_rows if row.vendor_id == invoice.vendor_id and row.total_amount is not None]
    if invoice.total_amount is not None:
        vendor_amounts.append(float(invoice.total_amount))
    benford = benford_analysis(vendor_amounts, min_sample=benford_min_sample or settings.benford_min_sample)
    if benford["tested"] and benford["flagged"]:
        signals.append({
            "type": "benford_deviation", "severity": 0.5, "weight": 0.45,
            "title": "First-digit distribution differs from Benford expectation",
            "explanation": benford["explanation"], "evidence": benford,
            "estimated_exposure": "0.00",
        })

    # Relationship graph stores only keyed tokens; raw bank/address values are never returned.
    vendor_rows = db.scalars(select(Vendor)).all()
    employee_rows = db.scalars(select(Employee).where(Employee.is_active.is_(True))).all()
    overlaps = relationship_overlaps(
        [{"id": row.id, "name": row.name, "address_token": row.address_token, "bank_token": row.bank_token} for row in vendor_rows],
        [{"id": row.id, "display_name": row.display_name, "address_token": row.address_token, "bank_token": row.bank_token} for row in employee_rows],
    )
    for signal in overlaps:
        if signal["evidence"].get("vendor_id") == invoice.vendor_id:
            signals.append({**signal, "weight": 1.0, "title": "Vendor-employee relationship requires review", "estimated_exposure": str(invoice.total_amount or Decimal("0"))})

    # A deterministic local Isolation Forest baseline is fitted only when history is sufficient.
    reference = _history_behavior_matrix(db, prior_history_rows)
    if invoice.total_amount is not None:
        amount_ratio = behavior["amount_to_median_ratio"] or 1.0
        count7 = behavior["invoices_in_prior_7_days"]
        days_previous = behavior["days_since_previous_invoice"] or 365
        po_lag = 0.0
        if invoice.purchase_order and invoice.issue_date:
            po_lag = float((invoice.issue_date - invoice.purchase_order.issue_date).days)
        price_variance = 0.0
        for detail in match_details.get("discrepancies", []):
            if detail.get("type") == "unit_price_variance":
                expected = float(detail.get("expected") or 0)
                actual = float(detail.get("actual") or 0)
                if expected:
                    price_variance = max(price_variance, (actual - expected) / expected)
        current_vector = build_behavior_vector(float(invoice.total_amount), amount_ratio, count7, days_previous, price_variance, po_lag)
        model, training_info = train_isolation_forest(reference, contamination=0.05, random_state=42, min_samples=20)
        if model is not None:
            reference_scores = model.decision_function(np.asarray(reference, dtype=float))
            isolation = score_isolation_forest(model, current_vector, reference)
            dataset_fingerprint = hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()[:10]
            version = f"iforest-local-{dataset_fingerprint}"
            model_versions["isolation_forest"] = version
            if not db.scalar(select(ModelVersion).where(ModelVersion.component == "isolation_forest_behavior", ModelVersion.version == version)):
                db.add(ModelVersion(
                    component="isolation_forest_behavior", version=version, status="ephemeral_baseline",
                    metrics={**training_info, "reference_score_min": float(np.min(reference_scores)), "reference_score_max": float(np.max(reference_scores)), "promotion_status": "not_promoted"},
                    data_version=dataset_fingerprint,
                ))
            if isolation["anomaly"]:
                signals.append({
                    "type": "isolation_forest_anomaly", "severity": max(0.55, isolation["severity_index"]), "weight": 0.65,
                    "title": "Unusual transaction behavior", "explanation": isolation["explanation"],
                    "evidence": {**isolation, "features": current_vector, "training_rows": training_info["training_rows"], "model_version": version},
                    "estimated_exposure": str(invoice.total_amount),
                })
        else:
            model_versions["isolation_forest"] = "not_scored_insufficient_history"

    score, band = aggregate_heuristic_risk(signals)
    return score, band, signals, model_versions


def _ensure_review_case(db: Session, invoice: Invoice, reason_codes: list[str], summary: str, risk: float) -> ReviewCase:
    open_case = db.scalar(select(ReviewCase).where(ReviewCase.invoice_id == invoice.id, ReviewCase.status.in_(["open", "in_review", "waiting_documents"])).order_by(ReviewCase.created_at.desc()))
    if open_case:
        open_case.reason_codes = sorted(set(open_case.reason_codes or []).union(reason_codes))
        open_case.summary = summary
        if risk >= 0.85:
            open_case.priority = "critical"
        elif risk >= 0.6:
            open_case.priority = "high"
        return open_case
    priority = "critical" if risk >= 0.85 else "high" if risk >= 0.6 else "normal"
    case = ReviewCase(invoice_id=invoice.id, status="open", priority=priority, reason_codes=sorted(set(reason_codes)), summary=summary)
    db.add(case)
    db.flush()
    return case


def _persist_fraud_alerts(db: Session, invoice: Invoice, assessment: FraudAssessment, signals: list[dict[str, Any]]) -> None:
    for signal in signals:
        if float(signal.get("severity", 0)) < 0.5:
            continue
        alert_type = signal.get("type", "unknown")
        existing = db.scalar(select(FraudAlert).where(FraudAlert.invoice_id == invoice.id, FraudAlert.alert_type == alert_type, FraudAlert.status == "open"))
        if existing:
            existing.severity = float(signal["severity"])
            existing.title = signal.get("title", alert_type)
            existing.explanation = signal.get("explanation", "")
            existing.evidence = signal.get("evidence", {})
            existing.assessment_id = assessment.id
        else:
            evidence = dict(signal.get("evidence", {}))
            evidence["estimated_exposure"] = signal.get("estimated_exposure", "0.00")
            db.add(FraudAlert(
                invoice_id=invoice.id, assessment_id=assessment.id, alert_type=alert_type,
                severity=float(signal["severity"]), title=signal.get("title", alert_type),
                explanation=signal.get("explanation", ""), evidence=evidence, status="open",
            ))


def _purchase_order_allocation_lock_statement(purchase_order_id: str):
    """Lock and refresh the PO row before reading cumulative invoice allocations.

    PostgreSQL row-locks this PO so two independent workers cannot both validate
    against the same stale allocation total. SQLite's session factory already
    serializes writers with BEGIN IMMEDIATE; its dialect ignores FOR UPDATE.
    """
    return (
        select(PurchaseOrder)
        .where(PurchaseOrder.id == purchase_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def evaluate_invoice(
    db: Session, invoice: Invoice, *, actor_id: str | None = None,
    additional_review_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Re-run matching and independent fraud controls against the current persisted extraction."""
    # Acquire the per-PO allocation lock before reading extraction/match history.
    # In PostgreSQL READ COMMITTED, the next statement sees any allocation committed
    # by the worker that held this lock first; SQLite begins writes IMMEDIATE.
    purchase_order = db.scalar(_purchase_order_allocation_lock_statement(invoice.purchase_order_id)) if invoice.purchase_order_id else None
    if purchase_order:
        _ = purchase_order.lines
    extraction = db.scalar(select(ExtractionResult).where(ExtractionResult.invoice_id == invoice.id).order_by(ExtractionResult.extraction_version.desc()).limit(1))
    errors = extraction.validation_errors if extraction else []
    receipts = db.scalars(select(GoodsReceipt).where(GoodsReceipt.purchase_order_id == purchase_order.id)).all() if purchase_order else []
    for receipt in receipts:
        _ = receipt.lines
    match_payload = {
        "id": invoice.id, "invoice_number": invoice.invoice_number, "vendor_id": invoice.vendor_id,
        "purchase_order_number": purchase_order.number if purchase_order else None, "currency": invoice.currency,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "subtotal": str(invoice.subtotal) if invoice.subtotal is not None else None,
        "tax_amount": str(invoice.tax_amount) if invoice.tax_amount is not None else None,
        "shipping_amount": str(invoice.shipping_amount), "discount_amount": str(invoice.discount_amount),
        "total_amount": str(invoice.total_amount) if invoice.total_amount is not None else None,
        "lines": [{
            "sku": line.sku, "description": line.description, "quantity": str(line.quantity),
            "unit_price": str(line.unit_price), "line_total": str(line.line_total),
        } for line in invoice.lines],
    }
    abs_row = db.get(ApplicationSetting, "tolerance_absolute")
    pct_row = db.get(ApplicationSetting, "tolerance_percent")
    mode_row = db.get(ApplicationSetting, "tolerance_policy")
    benford_row = db.get(ApplicationSetting, "benford_min_sample")
    abs_value = (abs_row.value or {}).get("amount", settings.tolerance_absolute) if abs_row else settings.tolerance_absolute
    pct_value = (pct_row.value or {}).get("fraction", settings.tolerance_percent) if pct_row else settings.tolerance_percent
    mode_value = (mode_row.value or {}).get("mode", settings.tolerance_policy) if mode_row else settings.tolerance_policy
    benford_minimum = int((benford_row.value or {}).get("count", settings.benford_min_sample)) if benford_row else settings.benford_min_sample
    policy = TolerancePolicy(absolute=Decimal(str(abs_value)), percent=Decimal(str(pct_value)), mode=str(mode_value))
    prior_invoice_quantities = _prior_invoice_quantities(db, purchase_order.id, invoice.id) if purchase_order else {}
    match_result = reconcile(
        match_payload, _po_payload(purchase_order), _receipt_payload(receipts),
        policy=policy, previous_invoice_quantities=prior_invoice_quantities,
    )
    db.add(InvoiceMatchResult(invoice_id=invoice.id, outcome=match_result["outcome"], details=match_result))
    db.flush()
    document_hash = invoice.documents[0].sha256 if invoice.documents else None
    score, band, signals, model_versions = _run_fraud_analysis(db, invoice, match_result, document_hash, benford_minimum)
    assessment = FraudAssessment(invoice_id=invoice.id, heuristic_score=score, risk_band=band, is_probability=False, signals=signals, model_versions=model_versions)
    db.add(assessment)
    db.flush()
    _persist_fraud_alerts(db, invoice, assessment, signals)
    reasons: list[str] = list(dict.fromkeys(additional_review_reasons or []))
    if errors:
        reasons.append("extraction_validation")
    currency_confidence = _field_confidence(extraction.extracted_fields if extraction else {}, "currency")
    if currency_confidence < 0.95:
        reasons.append("currency_confidence_below_policy")
    if invoice.extraction_confidence is None or invoice.extraction_confidence < 0.95:
        reasons.append("extraction_confidence_below_policy")
    if match_result["outcome"] not in {"MATCHED", "MATCHED_WITHIN_TOLERANCE"}:
        reasons.append("reconciliation_exception")
    if score >= 0.25:
        reasons.append("fraud_signal_requires_review")
    existing_open_case = db.scalar(select(ReviewCase.id).where(ReviewCase.invoice_id == invoice.id, ReviewCase.status.in_(["open", "in_review", "waiting_documents"])).limit(1))
    if reasons:
        invoice.workflow_status = "review_required"
        summary = f"Review required: {', '.join(reasons)}. Extraction confidence, match status, and heuristic fraud risk are separate controls."
        _ensure_review_case(db, invoice, reasons, summary, score)
    elif existing_open_case:
        invoice.workflow_status = "review_required"
    else:
        invoice.workflow_status = "ready_for_approval"
    invoice.heuristic_risk_score = score
    append_audit_event(db, actor_id=actor_id, action="invoice.analysis_completed", object_type="invoice", object_id=invoice.id, details={"match_outcome": match_result["outcome"], "heuristic_risk_score": score, "risk_band": band, "review_reasons": reasons})
    db.flush()
    return {"match": match_result, "heuristic_risk_score": score, "risk_band": band, "signals": signals, "review_reasons": reasons, "model_versions": model_versions}


def _claim_queued_job(db: Session, job_id: str, *, now: datetime | None = None) -> bool:
    """Atomically move one queued job to processing; concurrent workers can have one winner."""
    claimed_at = now or datetime.now(timezone.utc)
    result = db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id, ProcessingJob.state == "queued")
        .values(
            state="processing", stage="document_extraction",
            attempts=ProcessingJob.attempts + 1, started_at=claimed_at,
            heartbeat_at=claimed_at, error_message=None,
        )
    )
    return result.rowcount == 1


def _touch_processing_job(db: Session, job_id: str, attempt: int, stage: str, *, now: datetime | None = None) -> bool:
    """Refresh a processing lease only while this worker still owns the claimed attempt."""
    result = db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id, ProcessingJob.state == "processing", ProcessingJob.attempts == attempt)
        .values(stage=stage, heartbeat_at=now or datetime.now(timezone.utc))
    )
    return result.rowcount == 1


def _finish_processing_job(
    db: Session, job_id: str, attempt: int, *, state: str, stage: str,
    error_message: str | None = None, now: datetime | None = None,
) -> bool:
    """Fence completion/failure so a recovered stale worker cannot overwrite a newer state."""
    finished_at = now or datetime.now(timezone.utc)
    result = db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id, ProcessingJob.state == "processing", ProcessingJob.attempts == attempt)
        .values(
            state=state, stage=stage, error_message=error_message,
            finished_at=finished_at, heartbeat_at=finished_at,
        )
    )
    return result.rowcount == 1


def run_processing_job(job_id: str, session_factory, storage_dir: Path | None = None) -> None:
    """Idempotent worker entry point. Uses the DB session factory, so it also runs in Celery."""
    storage_root = (storage_dir or settings.storage_path).resolve()
    with session_factory() as db:
        job = db.get(ProcessingJob, job_id)
        if not job or job.state != "queued":
            return
        invoice = db.get(Invoice, job.invoice_id)
        if not invoice:
            job.state = "failed"
            job.error_message = "Linked invoice no longer exists."
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            return
        document = db.scalar(select(DocumentFile).where(DocumentFile.invoice_id == invoice.id).order_by(DocumentFile.id).limit(1))
        if not document:
            job.state = "failed"
            job.error_message = "No document is attached to this invoice."
            job.finished_at = datetime.now(timezone.utc)
            invoice.processing_status = "failed"
            db.commit()
            return
        if not _claim_queued_job(db, job_id):
            db.rollback()
            return
        db.refresh(job)
        claimed_attempt = job.attempts
        invoice.processing_status = "processing"
        db.commit()
        doc_path = Path(document.stored_path).resolve()
        if not doc_path.is_relative_to(storage_root):
            error = "Stored document path escapes the configured upload directory."
            now = datetime.now(timezone.utc)
            if _finish_processing_job(db, job_id, claimed_attempt, state="failed", stage="document_validation", error_message=error, now=now):
                invoice.processing_status, invoice.workflow_status = "failed", "review_required"
                _ensure_review_case(db, invoice, ["unsafe_document_path"], "The stored source path failed a security validation and requires review.", 0.0)
                append_audit_event(db, actor_id=None, action="invoice.processing_failed", object_type="invoice", object_id=invoice.id, details={"job_id": job.id, "reason_code": "unsafe_document_path"})
                db.commit()
            else:
                db.rollback()
            return
        try:
            payload = doc_path.read_bytes()
            extraction = process_document(payload, document.original_filename, document.mime_type)
            fields = extraction.fields
            previous_versions = db.query(ExtractionResult).filter_by(invoice_id=invoice.id).count()
            db.add(ExtractionResult(
                invoice_id=invoice.id, raw_text=extraction.raw_text,
                extracted_fields=fields, tokens=extraction.tokens,
                validation_errors=extraction.validation_errors,
                engine_name=extraction.engine_name, engine_version=extraction.engine_version,
                extraction_version=previous_versions + 1,
            ))
            vendor = _resolve_vendor(db, fields)
            upload_review_reasons: list[str] = []
            if invoice.source == "vendor_upload":
                bound_vendor = db.get(Vendor, invoice.vendor_id) if invoice.vendor_id else None
                if bound_vendor is None:
                    identity_reason = "vendor_identity_unverified"
                else:
                    identity_reason = _vendor_identity_review_reason(bound_vendor, fields, vendor)
                if identity_reason:
                    upload_review_reasons.append(identity_reason)
                append_audit_event(
                    db, actor_id=None,
                    action="invoice.vendor_identity_review_required" if identity_reason else "invoice.vendor_identity_verified",
                    object_type="invoice", object_id=invoice.id,
                    details={
                        "bound_vendor_id": invoice.vendor_id,
                        "extracted_match_vendor_id": vendor.id if vendor else None,
                        "reason_code": identity_reason,
                    },
                )
                # Deliberately retain the authenticated account's vendor binding. Extracted
                # document identity is evidence, never authority to reassign the invoice.
            else:
                invoice.vendor_id = vendor.id if vendor else None
            invoice.invoice_number = str(_field_value(fields, "invoice_number") or f"UNPARSED-{invoice.id[:8]}")
            invoice.currency = str(_field_value(fields, "currency") or "USD").upper()[:3]
            invoice.issue_date = _to_date(_field_value(fields, "issue_date"))
            invoice.due_date = _to_date(_field_value(fields, "due_date"))
            invoice.subtotal = D(_field_value(fields, "subtotal")) if _field_value(fields, "subtotal") is not None else None
            invoice.tax_amount = D(_field_value(fields, "tax_amount")) if _field_value(fields, "tax_amount") is not None else None
            invoice.shipping_amount = D(_field_value(fields, "shipping_amount") or "0.00")
            invoice.discount_amount = D(_field_value(fields, "discount_amount") or "0.00")
            total_value = _field_value(fields, "total_amount")
            invoice.total_amount = D(total_value) if total_value is not None else None
            po_number = _field_value(fields, "purchase_order_number")
            purchase_order = db.scalar(select(PurchaseOrder).where(PurchaseOrder.number == po_number)) if po_number else None
            invoice.purchase_order_id = purchase_order.id if purchase_order else None
            invoice.lines.clear()
            for field_line in fields.get("line_items", []):
                invoice.lines.append(InvoiceLine(
                    line_number=int(field_line.get("line_number", 1)),
                    sku=field_line.get("sku"), description=str(field_line.get("description") or "Unidentified line"),
                    quantity=D(field_line.get("quantity")), unit_price=D(field_line.get("unit_price")),
                    tax_rate=D(field_line.get("tax_rate")),
                    tax_amount=D(field_line.get("tax_amount")) if field_line.get("tax_amount") is not None else None,
                    line_total=D(field_line.get("line_total")),
                ))
            invoice.processing_status = "extracted"
            core_fields = ("vendor_name", "invoice_number", "issue_date", "total_amount")
            confidence_values = [_field_confidence(fields, name) for name in core_fields]
            invoice.extraction_confidence = round(sum(confidence_values) / len(confidence_values), 4)
            db.flush()
            if not _touch_processing_job(db, job_id, claimed_attempt, "fraud_analysis"):
                db.rollback()
                return
            db.commit()
            analysis = evaluate_invoice(db, invoice, additional_review_reasons=upload_review_reasons)
            # Payment execution is intentionally absent; passing checks is not payment authorization.
            invoice.processing_status = "completed"
            completed_at = datetime.now(timezone.utc)
            if not _finish_processing_job(db, job_id, claimed_attempt, state="completed", stage="complete", now=completed_at):
                db.rollback()
                return
            append_audit_event(
                db, actor_id=None, action="invoice.processing_completed", object_type="invoice", object_id=invoice.id,
                details={"job_id": job.id, "match_outcome": analysis["match"]["outcome"], "heuristic_risk_score": analysis["heuristic_risk_score"], "risk_band": analysis["risk_band"], "workflow_status": invoice.workflow_status},
            )
            db.commit()
        except (OCRUnavailable, ValueError, OSError) as exc:
            logger.exception("Invoice processing failed for %s", invoice.id)
            db.rollback()
            failed_at = datetime.now(timezone.utc)
            job = db.get(ProcessingJob, job_id)
            invoice = db.get(Invoice, job.invoice_id) if job else None
            if not job or not _finish_processing_job(
                db, job_id, claimed_attempt, state="failed", stage="document_extraction",
                error_message=str(exc)[:1000], now=failed_at,
            ):
                db.rollback()
                return
            if invoice:
                invoice.processing_status, invoice.workflow_status = "failed", "review_required"
                _ensure_review_case(db, invoice, ["processing_failure"], "Automated processing failed; reviewer attention required.", 0.0)
                append_audit_event(db, actor_id=None, action="invoice.processing_failed", object_type="invoice", object_id=invoice.id, details={"job_id": job.id, "error_type": type(exc).__name__})
            db.commit()
        except Exception as exc:
            logger.exception("Unexpected invoice processing exception for %s", invoice.id)
            db.rollback()
            job = db.get(ProcessingJob, job_id)
            invoice = db.get(Invoice, job.invoice_id) if job else None
            if not job or not _finish_processing_job(
                db, job_id, claimed_attempt, state="failed", stage="unexpected_error",
                error_message=f"Unexpected processing error: {type(exc).__name__}: {exc}"[:1000],
            ):
                db.rollback()
                return
            if invoice:
                invoice.processing_status, invoice.workflow_status = "failed", "review_required"
                _ensure_review_case(db, invoice, ["processing_failure"], "Automated processing failed unexpectedly; reviewer attention required.", 0.0)
                append_audit_event(db, actor_id=None, action="invoice.processing_failed", object_type="invoice", object_id=invoice.id, details={"job_id": job_id, "error_type": type(exc).__name__})
            db.commit()
