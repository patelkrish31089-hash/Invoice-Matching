from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO, BytesIO
import csv
import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, select, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.common import invoice_summary, invoice_summaries
from app.audit import append_audit_event, verify_audit_chain
from app.config import settings
from app.integrations.erp_sandbox import (
    EXTERNAL_SOURCE, ERPContractError, ERPNotConfigured, ERPProviderError,
    ERPSyncConflict, apply_sandbox_records, fetch_invoice_status, fetch_sandbox_records,
)
from app.db import get_db, requires_write_transaction
from app.models import (
    ApplicationSetting, AuditEvent, FraudAlert, FraudAssessment, ProcessingJob, ProcessingOutbox, Role, User, GoodsReceipt,
    GoodsReceiptLine, DocumentFile, Invoice, InvoiceMatchResult, ModelVersion, PurchaseOrder,
    PurchaseOrderLine, QuarantinedUpload, ReviewCase, UploadReceipt, Vendor,
)
from app.schemas import (
    GoodsReceiptCreateRequest, PurchaseOrderCreateRequest, SettingsUpdateRequest,
    OutboxRetryRequest, StaleProcessingJobRecoveryRequest, VendorCreateRequest, UserCreateRequest, UserRoleUpdateRequest,
)
from app.security import get_current_user, require_roles, hash_password
from app.outbox import dispatch_outbox_event, outbox_event_payload
from app.upload_recovery import recover_stale_upload_receipts

router = APIRouter(tags=["analytics, reference data, and administration"])
READ_ROLES = ("admin", "ap_clerk", "reviewer", "auditor")
WRITE_ROLES = ("admin", "ap_clerk")


def _vendor_safe(vendor: Vendor, db: Session | None = None) -> dict:
    return {
        "id": vendor.id, "code": vendor.code, "name": vendor.name,
        "tax_identifier_masked": ("••••" + vendor.tax_identifier[-4:]) if vendor.tax_identifier else None,
        "aliases": vendor.aliases, "is_active": vendor.is_active,
        "risk_tier": vendor.risk_tier, "created_at": vendor.created_at.isoformat(),
        "invoice_count": db.query(Invoice).filter(Invoice.vendor_id == vendor.id).count() if db else None,
    }


def _po_payload(po: PurchaseOrder) -> dict:
    return {
        "id": po.id, "number": po.number, "vendor_id": po.vendor_id,
        "vendor_name": po.vendor.name if po.vendor else None,
        "currency": po.currency, "status": po.status, "issue_date": po.issue_date.isoformat(),
        "expected_total": str(po.expected_total),
        "lines": [{"id": line.id, "line_number": line.line_number, "sku": line.sku, "description": line.description, "quantity": str(line.quantity), "unit_price": str(line.unit_price), "tax_rate": str(line.tax_rate)} for line in po.lines],
    }


def _grn_payload(grn: GoodsReceipt) -> dict:
    return {
        "id": grn.id, "number": grn.number, "purchase_order_id": grn.purchase_order_id,
        "purchase_order_number": grn.purchase_order.number if grn.purchase_order else None,
        "received_date": grn.received_date.isoformat(), "status": grn.status,
        "lines": [{"id": line.id, "purchase_order_line_id": line.purchase_order_line_id, "sku": line.sku, "description": line.description, "quantity": str(line.quantity)} for line in grn.lines],
    }


def _setting_value(db: Session, key: str, field: str, default):
    row = db.get(ApplicationSetting, key)
    return (row.value or {}).get(field, default) if row else default


def _currency_breakdown(values: dict[str, Decimal]) -> dict[str, str]:
    return {code: str(amount.quantize(Decimal("0.01"))) for code, amount in sorted(values.items())}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _spreadsheet_safe_cell(value):
    if not isinstance(value, str):
        return value
    trimmed = value.lstrip(" \t\r\n")
    if value.startswith(("\t", "\r", "\n")) or (trimmed and trimmed[0] in "=+-@"):
        return "'" + value
    return value

def _job_last_activity(job: ProcessingJob) -> datetime | None:
    return job.heartbeat_at or job.started_at or job.queued_at


def _ranked_latest_matches(): 
    return select(
        InvoiceMatchResult.invoice_id.label("invoice_id"),
        InvoiceMatchResult.outcome.label("outcome"),
        InvoiceMatchResult.details.label("details"),
        func.row_number().over(
            partition_by=InvoiceMatchResult.invoice_id,
            order_by=(InvoiceMatchResult.evaluated_at.desc(), InvoiceMatchResult.id.desc()),
        ).label("row_rank"),
    ).subquery()


def _ranked_latest_assessments():
    return select(
        FraudAssessment.invoice_id.label("invoice_id"),
        FraudAssessment.risk_band.label("risk_band"),
        func.row_number().over(
            partition_by=FraudAssessment.invoice_id,
            order_by=(FraudAssessment.analyzed_at.desc(), FraudAssessment.id.desc()),
        ).label("row_rank"),
    ).subquery()


@router.get("/analytics/overview")
def analytics_overview(db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    latest_matches = _ranked_latest_matches()
    latest_assessments = _ranked_latest_assessments()
    rows = db.execute(
        select(
            Invoice, latest_matches.c.outcome, latest_matches.c.details,
            latest_assessments.c.risk_band,
        )
        .outerjoin(latest_matches, and_(latest_matches.c.invoice_id == Invoice.id, latest_matches.c.row_rank == 1))
        .outerjoin(latest_assessments, and_(latest_assessments.c.invoice_id == Invoice.id, latest_assessments.c.row_rank == 1))
        .order_by(Invoice.created_at.desc())
    ).yield_per(1000)
    latest_outcomes = Counter()
    exposure_by_currency: dict[str, Decimal] = {}
    risk_distribution = Counter()
    total_invoices = 0
    pending = 0
    for invoice, outcome, match_details, risk_band in rows:
        total_invoices += 1
        if invoice.workflow_status in {"review_required", "processing", "waiting_documents", "escalated"}:
            pending += 1
        if outcome:
            latest_outcomes[outcome] += 1
            try:
                code = (invoice.currency or "UNK").upper()
                exposure_by_currency[code] = exposure_by_currency.get(code, Decimal("0")) + Decimal(str((match_details or {}).get("financial_exposure", "0")))
            except Exception:
                pass
        risk_distribution[risk_band or "unscored"] += 1
    open_cases = db.query(ReviewCase).filter(ReviewCase.status.in_(["open", "in_review", "waiting_documents", "escalated"])).count()
    open_alerts = db.query(FraudAlert).filter(FraudAlert.status == "open").count()
    failed_jobs = db.scalar(select(func.count()).select_from(ProcessingJob).where(ProcessingJob.state == "failed")) or 0
    recent = db.execute(
        select(Invoice.issue_date, Invoice.total_amount, Invoice.currency, Vendor.name)
        .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
        .where(Invoice.issue_date.is_not(None))
        .order_by(Invoice.issue_date.desc(), Invoice.created_at.desc())
        .limit(7)
    ).all()
    trend = [{
        "date": issue_date.isoformat(), "amount": float(amount or 0),
        "currency": currency_code or "UNK", "vendor": vendor_name or "Unresolved vendor",
    } for issue_date, amount, currency_code, vendor_name in reversed(recent)]
    exposure_breakdown = _currency_breakdown(exposure_by_currency)
    single_currency_exposure = next(iter(exposure_breakdown.values())) if len(exposure_breakdown) == 1 else ("0.00" if not exposure_breakdown else None)
    return {
        "total_invoices": total_invoices,
        "awaiting_review": pending,
        "matched_invoices": latest_outcomes.get("MATCHED", 0) + latest_outcomes.get("MATCHED_WITHIN_TOLERANCE", 0),
        "unmatched_invoices": sum(value for key, value in latest_outcomes.items() if key not in {"MATCHED", "MATCHED_WITHIN_TOLERANCE"}),
        "high_risk_alerts": open_alerts,
        "open_review_cases": open_cases,
        "processing_failure_count": int(failed_jobs),
        "estimated_discrepancy_exposure": single_currency_exposure,
        "estimated_discrepancy_exposure_by_currency": exposure_breakdown,
        "exposure_currency": next(iter(exposure_breakdown)) if len(exposure_breakdown) == 1 else None,
        "exposure_note": "No FX conversion is applied. Currency totals are kept separate; a single aggregate is omitted when multiple currencies are present.",
        "confirmed_prevented_payment_value": "0.00",
        "confirmed_prevented_payment_note": "No payment integration exists; prevented-loss value is not measured.",
        "risk_distribution": [{"band": key, "count": value} for key, value in sorted(risk_distribution.items())],
        "reconciliation_distribution": [{"outcome": key, "count": value} for key, value in sorted(latest_outcomes.items())],
        "recent_invoice_trend": trend,
        "demo_data_notice": "Seeded historical/reference records are synthetic and clearly labeled; uploads are evaluated separately.",
    }


@router.get("/analytics/fraud")
def fraud_analytics(db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    rows = db.execute(
        select(FraudAlert, Invoice.currency, Vendor.name)
        .join(Invoice, Invoice.id == FraudAlert.invoice_id)
        .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
        .where(FraudAlert.status == "open")
        .order_by(FraudAlert.created_at.desc())
    ).yield_per(1000)
    by_type = Counter()
    by_severity = Counter()
    top_vendors = Counter()
    exposure_by_invoice: dict[str, tuple[str, Decimal]] = {}
    open_count = 0
    for alert, raw_currency, vendor_name in rows:
        open_count += 1
        by_type[alert.alert_type] += 1
        by_severity["critical" if alert.severity >= 0.85 else "high" if alert.severity >= 0.6 else "medium" if alert.severity >= 0.25 else "low"] += 1
        top_vendors[vendor_name or "Unresolved"] += 1
        try:
            amount = max(Decimal("0"), Decimal(str((alert.evidence or {}).get("estimated_exposure", "0"))))
            code = (raw_currency or "UNK").upper()
            previous = exposure_by_invoice.get(alert.invoice_id)
            if previous is None or amount > previous[1]:
                exposure_by_invoice[alert.invoice_id] = (code, amount)
        except Exception:
            continue
    exposure_by_currency: dict[str, Decimal] = {}
    for code, amount in exposure_by_invoice.values():
        exposure_by_currency[code] = exposure_by_currency.get(code, Decimal("0")) + amount
    breakdown = _currency_breakdown(exposure_by_currency)
    single_currency_exposure = next(iter(breakdown.values())) if len(breakdown) == 1 else ("0.00" if not breakdown else None)
    min_sample = _setting_value(db, "benford_min_sample", "count", settings.benford_min_sample)
    return {
        "open_alerts": open_count,
        "alert_types": [{"type": key, "count": value} for key, value in by_type.most_common()],
        "severity_distribution": [{"severity": key, "count": value} for key, value in sorted(by_severity.items())],
        "vendor_breakdown": [{"vendor": key, "count": value} for key, value in top_vendors.most_common(8)],
        "estimated_alert_exposure": single_currency_exposure,
        "estimated_alert_exposure_by_currency": breakdown,
        "exposure_currency": next(iter(breakdown)) if len(breakdown) == 1 else None,
        "exposure_note": "Open alert exposure is estimated and grouped by invoice/currency; no FX conversion or loss claim is implied.",
        "benford_policy": {"minimum_sample": int(min_sample), "standalone_fraud_evidence": False},
        "risk_semantics": "Scores rank review priority. They are not calibrated probabilities or findings of fraud.",
    }


@router.get("/analytics/reconciliation")
def reconciliation_analytics(db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    outcomes = Counter()
    exceptions = Counter()
    latest_matches = _ranked_latest_matches()
    rows = db.execute(select(latest_matches.c.outcome, latest_matches.c.details).where(latest_matches.c.row_rank == 1))
    for outcome, details in rows:
        outcomes[outcome] += 1
        for item in (details or {}).get("discrepancies", []):
            exceptions[item.get("type", "unknown")] += 1
    policy = {
        "absolute": _setting_value(db, "tolerance_absolute", "amount", settings.tolerance_absolute),
        "percentage": _setting_value(db, "tolerance_percent", "fraction", settings.tolerance_percent),
        "mode": _setting_value(db, "tolerance_policy", "mode", settings.tolerance_policy),
    }
    return {"outcomes": [{"outcome": key, "count": value} for key, value in sorted(outcomes.items())], "exception_types": [{"type": key, "count": value} for key, value in exceptions.most_common()], "tolerance_policy": policy, "fraud_gate_independent": True}


@router.get("/analytics/performance")
def performance_analytics(db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    from statistics import median

    latency_sample_limit = 1000
    state_counts = dict(db.execute(
        select(ProcessingJob.state, func.count()).group_by(ProcessingJob.state)
    ).all())
    job_count = sum(state_counts.values())
    completed = int(state_counts.get("completed", 0))
    failed = int(state_counts.get("failed", 0))
    latency_rows = db.execute(
        select(ProcessingJob.started_at, ProcessingJob.finished_at)
        .where(ProcessingJob.started_at.is_not(None), ProcessingJob.finished_at.is_not(None))
        .order_by(ProcessingJob.finished_at.desc(), ProcessingJob.id.desc())
        .limit(latency_sample_limit)
    ).all()
    durations = []
    for started_at, finished_at in latency_rows:
        start, finish = _utc(started_at), _utc(finished_at)
        if start and finish and finish >= start:
            durations.append((finish - start).total_seconds() * 1000)
    sorted_durations = sorted(durations)
    p95 = sorted_durations[min(len(sorted_durations) - 1, int(0.95 * (len(sorted_durations) - 1)))] if sorted_durations else None
    return {
        "job_count": job_count, "completed_jobs": completed, "failed_jobs": failed,
        "queued_jobs": int(state_counts.get("queued", 0)), "processing_jobs": int(state_counts.get("processing", 0)),
        "failure_rate": round(failed / max(1, completed + failed), 4),
        "latency_sample_size": len(durations), "latency_sample_limit": latency_sample_limit,
        "median_processing_ms": round(float(median(durations)), 1) if durations else None,
        "p95_processing_ms": round(float(p95), 1) if p95 is not None else None,
        "latency_note": "Job-state counts aggregate across persisted history; latency percentiles use at most the latest 1,000 jobs with start/finish timestamps. Synthetic seed processing is included.",
        "ocr_provider": "PyMuPDF text layer + Tesseract CPU adapter; Tesseract availability depends on host installation.",
        "model_inference": "Rule baseline and optional local Isolation Forest only; LayoutLM weights are not bundled.",
    }


@router.get("/fraud/alerts")
def list_fraud_alerts(
    status_filter: str | None = Query(default="open", alias="status"),
    alert_type: str | None = None,
    minimum_severity: float | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES)),
):
    query = select(FraudAlert)
    if status_filter:
        query = query.where(FraudAlert.status == status_filter)
    if alert_type:
        query = query.where(FraudAlert.alert_type == alert_type)
    if minimum_severity is not None:
        query = query.where(FraudAlert.severity >= minimum_severity)
    total = len(db.scalars(query).all())
    rows = db.scalars(query.order_by(FraudAlert.severity.desc(), FraudAlert.created_at.desc()).offset(offset).limit(limit)).all()
    items = []
    for alert in rows:
        invoice = db.get(Invoice, alert.invoice_id)
        items.append({
            "id": alert.id, "invoice_id": alert.invoice_id,
            "invoice_number": invoice.invoice_number if invoice else None,
            "vendor": invoice.vendor.name if invoice and invoice.vendor else None,
            "amount": str(invoice.total_amount) if invoice and invoice.total_amount is not None else None,
            "currency": invoice.currency if invoice else None,
            "type": alert.alert_type, "severity": alert.severity,
            "title": alert.title, "explanation": alert.explanation,
            "evidence": alert.evidence, "status": alert.status,
            "created_at": alert.created_at.isoformat(),
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/fraud/alerts/{alert_id}")
def get_fraud_alert(alert_id: str, db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    alert = db.get(FraudAlert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Fraud alert not found")
    invoice = db.get(Invoice, alert.invoice_id)
    return {"id": alert.id, "type": alert.alert_type, "severity": alert.severity, "title": alert.title, "explanation": alert.explanation, "evidence": alert.evidence, "invoice": invoice_summary(db, invoice, include_detail=True) if invoice else None}


@router.get("/vendors")
def list_vendors(search: str | None = Query(default=None, max_length=120), db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    query = select(Vendor)
    if search:
        query = query.where(or_(Vendor.name.ilike(f"%{search}%"), Vendor.code.ilike(f"%{search}%")))
    return {"items": [_vendor_safe(vendor, db) for vendor in db.scalars(query.order_by(Vendor.name)).all()]}


@router.post("/vendors", status_code=201)
def create_vendor(payload: VendorCreateRequest, db: Session = Depends(get_db), user: User = Depends(require_roles(*WRITE_ROLES))):
    vendor = Vendor(code=payload.code.strip().upper(), name=payload.name.strip(), tax_identifier=payload.tax_identifier, aliases=payload.aliases, risk_tier=payload.risk_tier)
    db.add(vendor)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Vendor code already exists")
    append_audit_event(db, actor_id=user.id, action="vendor.created", object_type="vendor", object_id=vendor.id, details={"code": vendor.code, "name": vendor.name})
    db.commit()
    return _vendor_safe(vendor, db)


@router.get("/vendors/{vendor_id}")
def get_vendor(vendor_id: str, db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    vendor = db.get(Vendor, vendor_id)
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    invoices = db.scalars(select(Invoice).where(Invoice.vendor_id == vendor_id).order_by(Invoice.issue_date.desc()).limit(50)).all()
    return {**_vendor_safe(vendor, db), "invoices": invoice_summaries(db, invoices), "sensitive_identifiers": "masked; relationship detector stores only protected tokens"}


@router.get("/purchase-orders")
def list_purchase_orders(vendor_id: str | None = None, db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    query = select(PurchaseOrder)
    if vendor_id:
        query = query.where(PurchaseOrder.vendor_id == vendor_id)
    return {"items": [_po_payload(po) for po in db.scalars(query.order_by(PurchaseOrder.issue_date.desc())).all()]}


@router.post("/purchase-orders", status_code=201)
def create_purchase_order(payload: PurchaseOrderCreateRequest, db: Session = Depends(get_db), user: User = Depends(require_roles(*WRITE_ROLES))):
    vendor = db.get(Vendor, payload.vendor_id)
    if not vendor or not vendor.is_active:
        raise HTTPException(status_code=422, detail="An active vendor is required")
    expected = sum((line.quantity * line.unit_price for line in payload.lines), Decimal("0"))
    po = PurchaseOrder(number=payload.number, vendor_id=payload.vendor_id, currency=payload.currency.upper(), status=payload.status, issue_date=payload.issue_date, expected_total=expected.quantize(Decimal("0.01")))
    po.lines = [PurchaseOrderLine(**line.model_dump()) for line in payload.lines]
    db.add(po)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Purchase order number already exists")
    append_audit_event(db, actor_id=user.id, action="purchase_order.created", object_type="purchase_order", object_id=po.id, details={"number": po.number, "vendor_id": po.vendor_id, "line_count": len(po.lines)})
    db.commit()
    return _po_payload(po)


@router.get("/goods-receipts")
def list_goods_receipts(purchase_order_id: str | None = None, db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    query = select(GoodsReceipt)
    if purchase_order_id:
        query = query.where(GoodsReceipt.purchase_order_id == purchase_order_id)
    return {"items": [_grn_payload(grn) for grn in db.scalars(query.order_by(GoodsReceipt.received_date.desc())).all()]}


@router.post("/goods-receipts", status_code=201)
def create_goods_receipt(payload: GoodsReceiptCreateRequest, db: Session = Depends(get_db), user: User = Depends(require_roles(*WRITE_ROLES))):
    po = db.get(PurchaseOrder, payload.purchase_order_id)
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    valid_line_ids = {line.id for line in po.lines}
    for line in payload.lines:
        if line.purchase_order_line_id and line.purchase_order_line_id not in valid_line_ids:
            raise HTTPException(status_code=422, detail="Receipt line references a PO line from another order")
    grn = GoodsReceipt(number=payload.number, purchase_order_id=po.id, received_date=payload.received_date)
    grn.lines = [GoodsReceiptLine(**line.model_dump()) for line in payload.lines]
    db.add(grn)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Goods receipt number already exists")
    append_audit_event(db, actor_id=user.id, action="goods_receipt.created", object_type="goods_receipt", object_id=grn.id, details={"number": grn.number, "purchase_order_id": po.id, "line_count": len(grn.lines)})
    db.commit()
    return _grn_payload(grn)


@router.get("/admin/settings")
def get_settings(db: Session = Depends(get_db), _user: User = Depends(require_roles("admin"))):
    rows = {row.key: row.value for row in db.scalars(select(ApplicationSetting)).all()}
    return {
        "tolerance_absolute": rows.get("tolerance_absolute", {"amount": settings.tolerance_absolute}).get("amount", settings.tolerance_absolute),
        "tolerance_percent": rows.get("tolerance_percent", {"fraction": settings.tolerance_percent}).get("fraction", settings.tolerance_percent),
        "tolerance_policy": rows.get("tolerance_policy", {"mode": settings.tolerance_policy}).get("mode", settings.tolerance_policy),
        "benford_min_sample": rows.get("benford_min_sample", {"count": settings.benford_min_sample}).get("count", settings.benford_min_sample),
        "auto_payment_enabled": False,
        "notes": ["Settings affect screening only; they never initiate payments.", "Review thresholds require organization-specific validation before production use."],
    }


@router.patch("/admin/settings")
def update_settings(payload: SettingsUpdateRequest, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    values = payload.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=422, detail="At least one setting is required")
    settings_mapping = {
        "tolerance_absolute": ("amount", lambda value: str(value)),
        "tolerance_percent": ("fraction", lambda value: str(value)),
        "tolerance_policy": ("mode", lambda value: value),
        "benford_min_sample": ("count", lambda value: int(value)),
    }
    for key, value in values.items():
        row = db.get(ApplicationSetting, key)
        nested_key, convert = settings_mapping[key]
        data = dict(row.value) if row else {}
        data[nested_key] = convert(value)
        if row:
            row.value = data
            row.updated_by_id = user.id
        else:
            db.add(ApplicationSetting(key=key, value=data, updated_by_id=user.id))
    append_audit_event(db, actor_id=user.id, action="admin.settings_updated", object_type="application_settings", object_id="tolerance-policy", details={key: str(value) for key, value in values.items()})
    db.commit()
    return get_settings(db, _user=user)


@router.get("/admin/processing-jobs/stale")
def list_stale_processing_jobs(
    queued_minutes: int = Query(default=10, ge=5, le=1440),
    processing_minutes: int = Query(default=60, ge=5, le=1440),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db), _user: User = Depends(require_roles("admin")),
):
    now = datetime.now(timezone.utc)
    queued_cutoff = now - timedelta(minutes=queued_minutes)
    processing_cutoff = now - timedelta(minutes=processing_minutes)
    last_activity = func.coalesce(ProcessingJob.heartbeat_at, ProcessingJob.started_at, ProcessingJob.queued_at)
    stale_filter = or_(
        and_(ProcessingJob.state == "queued", ProcessingJob.queued_at <= queued_cutoff),
        and_(ProcessingJob.state == "processing", last_activity <= processing_cutoff),
    )
    rows = db.execute(
        select(ProcessingJob, Invoice.invoice_number)
        .join(Invoice, Invoice.id == ProcessingJob.invoice_id)
        .where(stale_filter)
        .order_by(last_activity.asc())
        .limit(limit)
    ).all()
    items = []
    for job, invoice_number in rows:
        activity = _utc(_job_last_activity(job))
        if activity is None:
            continue
        threshold = queued_minutes if job.state == "queued" else processing_minutes
        items.append({
            "job_id": job.id, "invoice_id": job.invoice_id, "invoice_number": invoice_number,
            "state": job.state, "stage": job.stage, "attempts": job.attempts,
            "last_activity_at": activity.isoformat(), "age_seconds": max(0, int((now - activity).total_seconds())),
            "stale_after_minutes": threshold,
        })
    return {
        "items": items, "total": len(items), "queued_threshold_minutes": queued_minutes,
        "processing_threshold_minutes": processing_minutes, "limit": limit,
        "automatic_recovery": False,
        "note": "Stale jobs are only identified here. Recovery marks a job failed after explicit administrator confirmation; it never retries automatically.",
    }


@router.post("/admin/processing-jobs/{job_id}/recover")
def recover_stale_processing_job(
    job_id: str, payload: StaleProcessingJobRecoveryRequest,
    db: Session = Depends(get_db), user: User = Depends(require_roles("admin")),
):
    job = db.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Processing job not found")
    if job.state != payload.expected_state:
        raise HTTPException(status_code=409, detail="Processing job state changed; refresh stale-job status before recovery")
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=payload.stale_after_minutes)
    previous_stage = job.stage
    previous_attempts = job.attempts
    activity = _utc(_job_last_activity(job))
    last_activity_column = func.coalesce(ProcessingJob.heartbeat_at, ProcessingJob.started_at, ProcessingJob.queued_at)
    stale_predicate = ProcessingJob.queued_at <= stale_before if payload.expected_state == "queued" else last_activity_column <= stale_before
    result = db.execute(
        update(ProcessingJob)
        .execution_options(synchronize_session=False)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.state == payload.expected_state,
            ProcessingJob.attempts == job.attempts,
            stale_predicate,
        )
        .values(
            state="failed", stage="stale_recovered",
            error_message="Processing job was marked stale by an administrator. Verify worker health before retrying.",
            heartbeat_at=now, finished_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Processing job is no longer stale or its state changed; refresh and review before retrying")

    invoice = db.get(Invoice, job.invoice_id)
    reason_code = "stale_queued_job" if payload.expected_state == "queued" else "stale_processing_job"
    if invoice:
        invoice.processing_status = "failed"
        invoice.workflow_status = "review_required"
        open_case = db.scalar(select(ReviewCase).where(
            ReviewCase.invoice_id == invoice.id,
            ReviewCase.status.in_(["open", "in_review", "waiting_documents", "escalated"]),
        ).order_by(ReviewCase.created_at.desc()).limit(1))
        summary = "A processing job exceeded its stale threshold and was marked failed by an administrator. Verify worker health before manually retrying."
        if open_case:
            open_case.reason_codes = sorted(set(open_case.reason_codes or []).union({reason_code}))
            open_case.summary = summary
        else:
            db.add(ReviewCase(
                invoice_id=invoice.id, status="open", priority="normal",
                reason_codes=[reason_code], summary=summary,
            ))
    append_audit_event(
        db, actor_id=user.id, action="processing_job.stale_recovered",
        object_type="processing_job", object_id=job.id,
        details={
            "invoice_id": job.invoice_id, "previous_state": payload.expected_state,
            "previous_stage": previous_stage, "attempts": previous_attempts,
            "stale_after_minutes": payload.stale_after_minutes,
            "last_activity_at": activity.isoformat() if activity else None,
            "reason": payload.reason,
        },
    )
    db.commit()
    return {
        "job_id": job.id, "invoice_id": job.invoice_id, "state": "failed",
        "message": "The stale job was marked failed and sent to human review. No automatic retry occurred; retry processing manually after verifying worker health.",
    }


@router.get("/admin/processing-outbox")
def list_processing_outbox(
    state: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db), _user: User = Depends(require_roles("admin")),
):
    if state is not None and state not in {"pending", "published", "dead"}:
        raise HTTPException(status_code=422, detail="state must be pending, published, or dead")
    counts = dict(db.execute(
        select(ProcessingOutbox.state, func.count())
        .group_by(ProcessingOutbox.state)
    ).all())
    query = select(ProcessingOutbox).order_by(ProcessingOutbox.created_at.desc(), ProcessingOutbox.id.desc()).limit(limit)
    if state:
        query = query.where(ProcessingOutbox.state == state)
    events = db.scalars(query).all()
    return {
        "counts": {key: int(counts.get(key, 0)) for key in ("pending", "published", "dead")},
        "items": [outbox_event_payload(event) for event in events],
        "limit": limit,
        "note": "Outbox delivery is at-least-once. Worker job claims fence duplicate messages; event payloads expose identifiers and safe error types only.",
    }


@router.post("/admin/processing-outbox/{event_id}/retry", status_code=202)
def retry_processing_outbox(
    event_id: str, payload: OutboxRetryRequest, request: Request,
    background_tasks: BackgroundTasks, db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    event = db.get(ProcessingOutbox, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Processing outbox event not found")
    now = datetime.now(timezone.utc)
    if event.state == "published":
        raise HTTPException(status_code=409, detail="The processing event has already been dispatched")
    if event.locked_until and (_utc(event.locked_until) or now) > now:
        raise HTTPException(status_code=409, detail="The event is currently claimed by a dispatcher; retry after its claim expires")
    job = db.get(ProcessingJob, event.job_id)
    invoice = db.get(Invoice, event.invoice_id)
    if not job or not invoice:
        raise HTTPException(status_code=409, detail="The outbox event has no recoverable invoice/job")
    if job.state == "failed" and job.stage != "dispatch":
        raise HTTPException(status_code=409, detail="The job failed during processing; create a new processing attempt instead")
    if job.state not in {"queued", "failed"}:
        raise HTTPException(status_code=409, detail=f"The job is {job.state}; it cannot be redispatched")
    if job.state == "failed":
        job.state, job.stage, job.error_message = "queued", "queued", None
        job.started_at, job.finished_at, job.heartbeat_at = None, None, None
        invoice.processing_status, invoice.workflow_status = "queued", "processing"
    event.state, event.attempts = "pending", 0
    event.available_at, event.locked_until, event.claim_token = now, None, None
    event.published_at, event.last_error_type = None, None
    append_audit_event(
        db, actor_id=user.id, action="processing_outbox.retry_requested",
        object_type="processing_outbox", object_id=event.id,
        details={"job_id": job.id, "invoice_id": invoice.id, "reason": payload.reason.strip()},
    )
    db.commit()
    event_id, job_id = event.id, job.id
    db.rollback()
    dispatch_warning = None
    if settings.task_mode.lower() == "celery":
        dispatch_warning = dispatch_outbox_event(
            request.app.state.session_factory, request.app.state.storage_dir, event_id, force=True,
        )
    else:
        background_tasks.add_task(
            dispatch_outbox_event, request.app.state.session_factory,
            request.app.state.storage_dir, event_id, force=True,
        )
    event = db.get(ProcessingOutbox, event_id)
    job = db.get(ProcessingJob, job_id)
    if event:
        db.refresh(event)
    if job:
        db.refresh(job)
    return {
        "event_id": event_id, "job_id": job_id, "job_state": job.state if job else None,
        "dispatch_state": event.state if event else None,
        "dispatch_attempts": event.attempts if event else None,
        "dispatch_warning": "The event remains queued for bounded retry." if dispatch_warning else None,
        "message": "The durable processing event was reset and is available to the dispatcher. No payment is triggered.",
    }


@router.get("/admin/audit-logs")
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
    action: str | None = None, object_id: str | None = None,
    db: Session = Depends(get_db), _user: User = Depends(require_roles("admin", "auditor")),
):
    query = select(AuditEvent)
    if action:
        query = query.where(AuditEvent.action == action)
    if object_id:
        query = query.where(AuditEvent.object_id == object_id)
    total = len(db.scalars(query).all())
    events = db.scalars(query.order_by(AuditEvent.sequence.desc()).offset(offset).limit(limit)).all()
    return {"items": [{
        "sequence": event.sequence, "actor_id": event.actor_id, "action": event.action,
        "object_type": event.object_type, "object_id": event.object_id,
        "details": event.details, "occurred_at": event.occurred_at.isoformat(),
        "previous_hash": event.previous_hash, "event_hash": event.event_hash,
    } for event in events], "total": total, "limit": limit, "offset": offset, "append_only_api": True}


@router.get("/admin/audit-logs/verify")
def audit_integrity(db: Session = Depends(get_db), _user: User = Depends(require_roles("admin", "auditor"))):
    return verify_audit_chain(db)


@router.get("/admin/model-versions")
def list_model_versions(db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    models = db.scalars(select(ModelVersion).order_by(ModelVersion.created_at.desc())).all()
    return {"items": [{"id": item.id, "component": item.component, "version": item.version, "status": item.status, "artifact_uri": item.artifact_uri, "metrics": item.metrics, "data_version": item.data_version, "created_at": item.created_at.isoformat()} for item in models]}


@router.post("/admin/integrations/erp/sync")
def sync_erp_sandbox_reference_data(
    db: Session = Depends(get_db), user: User = Depends(require_roles("admin")),
):
    """Read vendors, purchase orders and receipts from an explicitly configured REST sandbox."""
    try:
        # Credential and URL validation occurs before constructing a client or making a request.
        records = fetch_sandbox_records(
            settings.erp_sandbox_base_url,
            settings.erp_sandbox_token,
            timeout_seconds=settings.erp_sandbox_timeout_seconds,
        )
    except ERPNotConfigured as exc:
        raise HTTPException(status_code=503, detail="ERP sandbox sync is not configured; no network request was made.") from exc
    except (ERPProviderError, ERPContractError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        counts = apply_sandbox_records(db, records, source=EXTERNAL_SOURCE)
        append_audit_event(
            db, actor_id=user.id, action="erp.sandbox_reference_sync_completed",
            object_type="integration", object_id=EXTERNAL_SOURCE,
            details={"counts": counts, "read_only_scope": ["vendors", "purchase_orders", "goods_receipts"]},
        )
        db.commit()
    except ERPSyncConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ERPContractError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="ERP reference data conflicted with a concurrent update; no partial sync was retained.") from None
    except Exception:
        db.rollback()
        raise
    return {
        "source": EXTERNAL_SOURCE,
        "mode": "read_only_reference_sync",
        "counts": counts,
        "payment_execution": False,
        "invoice_status_submission": False,
    }


@router.get("/admin/integrations/erp/invoices/{invoice_id}/status")
@requires_write_transaction
def read_erp_invoice_status(
    invoice_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles("admin")),
):
    """Fetch a provider-reported invoice status without changing local workflow or payment state."""
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if not invoice.vendor or not invoice.invoice_number:
        raise HTTPException(status_code=422, detail="Invoice needs a resolved vendor and invoice number for a status lookup")
    try:
        result = fetch_invoice_status(
            settings.erp_sandbox_base_url,
            settings.erp_sandbox_token,
            invoice.invoice_number,
            invoice.vendor.code,
            timeout_seconds=settings.erp_sandbox_timeout_seconds,
        )
    except ERPNotConfigured as exc:
        raise HTTPException(status_code=503, detail="ERP sandbox sync is not configured; no network request was made.") from exc
    except (ERPProviderError, ERPContractError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    append_audit_event(
        db, actor_id=user.id, action="erp.invoice_status_read",
        object_type="invoice", object_id=invoice.id,
        details={"provider": EXTERNAL_SOURCE, "found": bool(result["found"]), "read_only": True},
    )
    db.commit()
    return result


@router.get("/admin/system-health")
def system_health(db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES))):
    from app.services.document_processing import tesseract_available
    db.execute(select(1))
    return {"status": "ok", "database": "connected", "ocr_tesseract_available": tesseract_available(), "ocr_cpu_fallback": "PDF text layer available; scanned pages require system Tesseract", "task_mode": settings.task_mode, "auto_payment_enabled": False}


@router.get("/admin/storage-integrity")
def storage_integrity(request: Request, db: Session = Depends(get_db), _user: User = Depends(require_roles("admin"))):
    """Read-only comparison of database document references and files within the upload root."""
    storage_root = Path(request.app.state.storage_dir).resolve()
    sample_limit = 10
    scan_limit = 20_000
    documents = db.scalars(select(DocumentFile)).all()
    referenced_paths: set[Path] = set()
    missing_samples: list[dict] = []
    unsafe_samples: list[dict] = []
    missing_count = 0
    unsafe_reference_count = 0
    for document in documents:
        try:
            stored_path = Path(document.stored_path).resolve(strict=False)
            if not stored_path.is_relative_to(storage_root):
                unsafe_reference_count += 1
                if len(unsafe_samples) < sample_limit:
                    unsafe_samples.append({"document_id": document.id, "invoice_id": document.invoice_id, "filename": document.original_filename})
                continue
            if not stored_path.is_file():
                missing_count += 1
                if len(missing_samples) < sample_limit:
                    missing_samples.append({"document_id": document.id, "invoice_id": document.invoice_id, "filename": document.original_filename})
                continue
            referenced_paths.add(stored_path)
        except (OSError, RuntimeError, ValueError):
            unsafe_reference_count += 1
            if len(unsafe_samples) < sample_limit:
                unsafe_samples.append({"document_id": document.id, "invoice_id": document.invoice_id, "filename": document.original_filename})

    scanned_files = 0
    orphan_count = 0
    unsafe_disk_entries = 0
    scan_truncated = False
    orphan_samples: list[dict] = []
    if storage_root.is_dir():
        for current_dir, directory_names, filenames in os.walk(storage_root, followlinks=False):
            current = Path(current_dir)
            directory_names[:] = [name for name in directory_names if not (current / name).is_symlink()]
            for filename in filenames:
                candidate = current / filename
                if candidate.is_symlink():
                    unsafe_disk_entries += 1
                    continue
                try:
                    if not candidate.is_file():
                        continue
                    resolved = candidate.resolve(strict=False)
                    if not resolved.is_relative_to(storage_root):
                        unsafe_disk_entries += 1
                        continue
                except (OSError, RuntimeError, ValueError):
                    unsafe_disk_entries += 1
                    continue
                if scanned_files >= scan_limit:
                    scan_truncated = True
                    break
                scanned_files += 1
                if resolved not in referenced_paths:
                    orphan_count += 1
                    if len(orphan_samples) < sample_limit:
                        orphan_samples.append({"relative_path": str(resolved.relative_to(storage_root)), "size_bytes": candidate.stat().st_size})
            if scan_truncated:
                break

    needs_attention = bool(missing_count or unsafe_reference_count or orphan_count or unsafe_disk_entries)
    status = "partial" if scan_truncated else ("attention_required" if needs_attention else "ok")
    return {
        "status": status, "checked_at": datetime.now(timezone.utc).isoformat(),
        "database_documents": len(documents), "disk_files_scanned": scanned_files,
        "missing_referenced_files": missing_count, "orphan_files": orphan_count,
        "unsafe_database_paths": unsafe_reference_count, "unsafe_disk_entries": unsafe_disk_entries,
        "scan_limit": scan_limit, "scan_truncated": scan_truncated, "sample_limit": sample_limit,
        "missing_samples": missing_samples, "orphan_samples": orphan_samples, "unsafe_path_samples": unsafe_samples,
        "note": "Read-only snapshot comparison. No files were changed. Orphan and disk counts are lower bounds if scanning was truncated; pause uploads and workers for a stable reconciliation.",
    }


@router.get("/admin/quarantined-uploads")
@requires_write_transaction
def list_quarantined_uploads(
    request: Request,
    status: str = Query("quarantined", pattern="^(quarantined|released|removed|all)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    query = select(QuarantinedUpload)
    if status != "all":
        query = query.where(QuarantinedUpload.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(QuarantinedUpload.created_at.desc()).offset(offset).limit(limit)).all()
    append_audit_event(
        db, actor_id=user.id, action="document.quarantine_listed",
        object_type="quarantined_upload", object_id="collection",
        details={"status_filter": status, "returned": len(rows), "offset": offset},
    )
    db.commit()
    quarantine_root = Path(request.app.state.quarantine_dir).resolve()

    def _storage_status(row: QuarantinedUpload) -> str:
        candidate = Path(row.quarantine_path)
        try:
            if candidate.is_symlink():
                return "unsafe_path"
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(quarantine_root):
                return "unsafe_path"
            return "present" if resolved.is_file() else "missing"
        except (OSError, RuntimeError, ValueError):
            return "missing"

    return {
        "items": [
            {
                "id": row.id, "source": row.source, "original_filename": row.original_filename,
                "sha256": row.sha256, "byte_size": row.byte_size, "scanner": row.scanner_name,
                "scanner_version": row.scanner_version, "detection": row.detection,
                "status": row.status, "storage_status": _storage_status(row), "created_at": row.created_at.isoformat(),
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "resolution_reason": row.resolution_reason,
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "download_available": False,
        "note": "Quarantined bytes remain outside invoice storage and are never served by document routes. Automated release is disabled; follow the organization incident-response process.",
    }


@router.get("/admin/upload-recovery")
@requires_write_transaction
def list_upload_recovery_receipts(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    rows = db.scalars(
        select(UploadReceipt)
        .where(UploadReceipt.state.in_(("receiving", "staged", "abandoned")))
        .order_by(UploadReceipt.updated_at.desc())
        .limit(limit)
    ).all()
    append_audit_event(
        db, actor_id=user.id, action="upload.recovery_status_listed",
        object_type="upload_receipt", object_id="collection",
        details={"returned": len(rows), "limit": limit},
    )
    db.commit()
    return {
        "items": [
            {
                "id": row.id, "source": row.source, "state": row.state, "original_filename": row.original_filename,
                "sha256": row.content_sha256, "byte_size": row.byte_size,
                "invoice_id": row.invoice_id, "last_error_type": row.last_error_type,
                "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
            }
            for row in rows
        ],
        "automatic_startup_recovery": True,
        "note": "Storage paths and request idempotency keys are not returned. Only stale unreferenced files are removed; committed document bytes are retained.",
    }


@router.post("/admin/upload-recovery/run")
def run_upload_recovery(
    request: Request,
    older_than_seconds: int = Query(3600, ge=60, le=86_400),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    result = recover_stale_upload_receipts(
        db, Path(request.app.state.storage_dir), actor_id=user.id,
        older_than_seconds=older_than_seconds, limit=limit,
    )
    append_audit_event(
        db, actor_id=user.id, action="upload.recovery_run",
        object_type="upload_receipt", object_id="recovery-run",
        details={"examined": result["examined"], "abandoned": result["abandoned"], "recovered_committed": result["recovered_committed"], "files_removed": result["files_removed"], "grace_seconds": older_than_seconds},
    )
    db.commit()
    return {"status": "completed", **result, "note": "Committed document bytes were preserved; stale unreferenced staging/final files may have been removed so the same upload key or signed email event can be retried safely."}


def _invoice_csv_chunks(db: Session):
    output = StringIO()
    writer = csv.writer(output)

    def take_chunk() -> str:
        chunk = output.getvalue()
        output.seek(0)
        output.truncate(0)
        return chunk

    writer.writerow(["invoice_id", "invoice_number", "vendor", "issue_date", "currency", "total_amount", "workflow_status", "match_outcome", "heuristic_risk_score", "synthetic_demo_data"])
    yield take_chunk()
    latest_matches = _ranked_latest_matches()
    rows = db.execute(
        select(
            Invoice.id, Invoice.invoice_number, Vendor.name, Invoice.issue_date,
            Invoice.currency, Invoice.total_amount, Invoice.workflow_status,
            latest_matches.c.outcome, Invoice.heuristic_risk_score, Invoice.is_synthetic,
        )
        .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
        .outerjoin(latest_matches, and_(latest_matches.c.invoice_id == Invoice.id, latest_matches.c.row_rank == 1))
        .order_by(Invoice.created_at.desc())
    ).yield_per(1000)
    for row in rows:
        writer.writerow(_spreadsheet_safe_cell(value) for value in row)
        yield take_chunk()


@router.get("/reports/invoices.csv")
def export_invoices_csv(db: Session = Depends(get_db), _user: User = Depends(require_roles("admin", "ap_clerk", "reviewer", "auditor"))):
    return StreamingResponse(_invoice_csv_chunks(db), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=invoice-register.csv"})


def _review_csv_chunks(db: Session):
    output = StringIO()
    writer = csv.writer(output)

    def take_chunk() -> str:
        chunk = output.getvalue()
        output.seek(0)
        output.truncate(0)
        return chunk

    writer.writerow(["case_id", "invoice_number", "vendor", "priority", "status", "reason_codes", "assigned_to", "created_at"])
    yield take_chunk()
    rows = db.execute(
        select(
            ReviewCase.id, Invoice.invoice_number, Vendor.name, ReviewCase.priority,
            ReviewCase.status, ReviewCase.reason_codes, User.display_name, ReviewCase.created_at,
        )
        .join(Invoice, Invoice.id == ReviewCase.invoice_id)
        .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
        .outerjoin(User, User.id == ReviewCase.assigned_to_id)
        .order_by(ReviewCase.created_at.desc())
    ).yield_per(1000)
    for case_id, invoice_number, vendor_name, priority, status, reason_codes, assigned_to, created_at in rows:
        values = [case_id, invoice_number or "", vendor_name or "", priority, status, ";".join(reason_codes or []), assigned_to or "Unassigned", created_at.isoformat()]
        writer.writerow(_spreadsheet_safe_cell(value) for value in values)
        yield take_chunk()


@router.get("/reports/review-cases.csv")
def export_review_csv(db: Session = Depends(get_db), _user: User = Depends(require_roles("admin", "reviewer", "auditor"))):
    return StreamingResponse(_review_csv_chunks(db), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=review-cases.csv"})


@router.get("/reports/operations.pdf")
def export_operations_pdf(db: Session = Depends(get_db), _user: User = Depends(require_roles("admin", "reviewer", "auditor"))):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    metrics = analytics_overview(db, _user=_user)
    story = [Paragraph("LedgerLens · AP operations snapshot", getSampleStyleSheet()["Title"]), Spacer(1, 12)]
    story.append(Paragraph("Generated from persisted application records. Seeded demo/reference data is synthetic. This report does not represent confirmed fraud losses or payments prevented.", getSampleStyleSheet()["BodyText"]))
    story.append(Spacer(1, 12))
    exposure_text = " · ".join(f"{code} {amount}" for code, amount in metrics.get("estimated_discrepancy_exposure_by_currency", {}).items()) or "0.00"
    table = Table([["Metric", "Value"], ["Invoices", str(metrics["total_invoices"])], ["Awaiting review", str(metrics["awaiting_review"])], ["Open review cases", str(metrics["open_review_cases"])], ["Open alerts", str(metrics["high_risk_alerts"])], ["Estimated discrepancy exposure (no FX conversion)", exposure_text], ["Confirmed prevented-payment value", "Not measured; no payment integration"]], colWidths=[3.7 * 72, 2.3 * 72])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17263b")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#ccd4df")), ("PADDING", (0, 0), (-1, -1), 7)]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return Response(buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=ledgerlens-operations-report.pdf"})
