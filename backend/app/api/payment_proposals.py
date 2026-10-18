from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.db import get_db
from app.models import (
    AuditEvent,
    DocumentFile,
    ExtractionResult,
    FraudAlert,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceLine,
    InvoiceMatchResult,
    PaymentProposal,
    ProcessingJob,
    PurchaseOrder,
    PurchaseOrderLine,
    ReviewCase,
    ReviewDecision,
    UploadReceipt,
    User,
    Vendor,
)
from app.schemas import (
    PaymentProposalCancelRequest,
    PaymentProposalCreateRequest,
    PaymentProposalDecisionRequest,
    PaymentProposalHandoffRequest,
)
from app.security import require_roles


router = APIRouter(prefix="/payment-proposals", tags=["non-executable payment proposals"])
OPEN_REVIEW_STATUSES = ("open", "in_review", "waiting_documents", "escalated")
MATCHED_OUTCOMES = {"MATCHED", "MATCHED_WITHIN_TOLERANCE"}
INVOICE_CONTRIBUTION_ACTIONS = (
    "invoice.uploaded",
    "invoice.extraction_corrected",
    "invoice.reprocess_requested",
    "invoice.processing_dispatch_retried",
)
READ_ROLES = ("admin", "ap_clerk", "approver", "reviewer", "auditor")


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _latest_match(db: Session, invoice_id: str) -> InvoiceMatchResult | None:
    return db.scalar(
        select(InvoiceMatchResult)
        .where(InvoiceMatchResult.invoice_id == invoice_id)
        .order_by(InvoiceMatchResult.evaluated_at.desc(), InvoiceMatchResult.id.desc())
        .limit(1)
    )


def _approved_review_decision(db: Session, invoice_id: str) -> ReviewDecision | None:
    return db.scalar(
        select(ReviewDecision)
        .join(ReviewCase, ReviewCase.id == ReviewDecision.case_id)
        .where(
            ReviewCase.invoice_id == invoice_id,
            ReviewCase.status == "approved",
            ReviewDecision.decision == "approved",
        )
        .order_by(ReviewDecision.created_at.desc(), ReviewDecision.id.desc())
        .limit(1)
    )


def _validate_invoice_eligibility(
    db: Session, invoice: Invoice,
) -> tuple[Vendor, InvoiceMatchResult, ReviewDecision | None]:
    """Re-check the payment-facing state at proposal, approval, and handoff time."""
    if invoice.processing_status != "completed":
        raise HTTPException(status_code=409, detail="Invoice processing must be complete before a payment proposal can be considered")
    if invoice.workflow_status not in {"ready_for_approval", "approved_by_reviewer"}:
        raise HTTPException(status_code=409, detail="Invoice workflow state is not eligible for a payment proposal")

    match = _latest_match(db, invoice.id)
    if not match:
        raise HTTPException(status_code=409, detail="A current three-way match result is required before a payment proposal")
    review_decision = _approved_review_decision(db, invoice.id)
    if invoice.workflow_status == "ready_for_approval" and match.outcome.upper() not in MATCHED_OUTCOMES:
        raise HTTPException(status_code=409, detail="A matching exception requires a recorded human review approval before proposal creation")
    if invoice.workflow_status == "approved_by_reviewer" and not review_decision:
        raise HTTPException(status_code=409, detail="A reviewer-approved invoice must have a durable approved review decision")

    open_case_id = db.scalar(
        select(ReviewCase.id)
        .where(ReviewCase.invoice_id == invoice.id, ReviewCase.status.in_(OPEN_REVIEW_STATUSES))
        .limit(1)
    )
    if open_case_id:
        raise HTTPException(status_code=409, detail="An unresolved review case prevents a payment proposal")
    open_alert_id = db.scalar(
        select(FraudAlert.id)
        .where(FraudAlert.invoice_id == invoice.id, FraudAlert.status == "open")
        .limit(1)
    )
    if open_alert_id:
        raise HTTPException(status_code=409, detail="An unresolved fraud alert prevents a payment proposal")

    vendor = db.get(Vendor, invoice.vendor_id) if invoice.vendor_id else None
    if not vendor or not vendor.is_active:
        raise HTTPException(status_code=409, detail="A resolved active vendor is required before a payment proposal")
    if not invoice.invoice_number.strip():
        raise HTTPException(status_code=409, detail="A verified invoice number is required before a payment proposal")
    if invoice.total_amount is None or invoice.total_amount <= Decimal("0"):
        raise HTTPException(status_code=409, detail="A positive invoice total is required before a payment proposal")
    currency = (invoice.currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise HTTPException(status_code=409, detail="Invoice currency must be a three-letter code before a payment proposal")
    return vendor, match, review_decision


def _purchase_order_snapshot(db: Session, invoice: Invoice) -> dict[str, Any] | None:
    if not invoice.purchase_order_id:
        return None
    purchase_order = db.get(PurchaseOrder, invoice.purchase_order_id)
    if not purchase_order:
        return {"missing_purchase_order_id": invoice.purchase_order_id}
    po_lines = db.scalars(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.purchase_order_id == purchase_order.id)
        .order_by(PurchaseOrderLine.line_number.asc(), PurchaseOrderLine.id.asc())
    ).all()
    receipts = db.scalars(
        select(GoodsReceipt)
        .where(GoodsReceipt.purchase_order_id == purchase_order.id)
        .order_by(GoodsReceipt.number.asc(), GoodsReceipt.id.asc())
    ).all()
    receipt_snapshots: list[dict[str, Any]] = []
    for receipt in receipts:
        receipt_lines = db.scalars(
            select(GoodsReceiptLine)
            .where(GoodsReceiptLine.goods_receipt_id == receipt.id)
            .order_by(GoodsReceiptLine.id.asc())
        ).all()
        receipt_snapshots.append({
            "id": receipt.id,
            "number": receipt.number,
            "status": receipt.status,
            "received_date": _iso(receipt.received_date),
            "updated_at": _iso(receipt.updated_at),
            "lines": [{
                "id": line.id,
                "purchase_order_line_id": line.purchase_order_line_id,
                "sku": line.sku,
                "description": line.description,
                "quantity": _decimal(line.quantity),
            } for line in receipt_lines],
        })
    return {
        "id": purchase_order.id,
        "number": purchase_order.number,
        "vendor_id": purchase_order.vendor_id,
        "currency": purchase_order.currency,
        "status": purchase_order.status,
        "issue_date": _iso(purchase_order.issue_date),
        "expected_total": _decimal(purchase_order.expected_total),
        "updated_at": _iso(purchase_order.updated_at),
        "lines": [{
            "id": line.id,
            "line_number": line.line_number,
            "sku": line.sku,
            "description": line.description,
            "quantity": _decimal(line.quantity),
            "unit_price": _decimal(line.unit_price),
            "tax_rate": _decimal(line.tax_rate),
        } for line in po_lines],
        "receipts": receipt_snapshots,
    }


def _invoice_fingerprint(
    db: Session,
    invoice: Invoice,
    vendor: Vendor,
    match: InvoiceMatchResult,
    review_decision: ReviewDecision | None,
) -> str:
    """Hash all payment-relevant local state; never expose the underlying snapshot."""
    lines = db.scalars(
        select(InvoiceLine)
        .where(InvoiceLine.invoice_id == invoice.id)
        .order_by(InvoiceLine.line_number.asc(), InvoiceLine.id.asc())
    ).all()
    documents = db.scalars(
        select(DocumentFile)
        .where(DocumentFile.invoice_id == invoice.id)
        .order_by(DocumentFile.id.asc())
    ).all()
    extraction = db.scalar(
        select(ExtractionResult)
        .where(ExtractionResult.invoice_id == invoice.id)
        .order_by(ExtractionResult.extraction_version.desc(), ExtractionResult.id.desc())
        .limit(1)
    )
    job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.invoice_id == invoice.id)
        .order_by(ProcessingJob.queued_at.desc(), ProcessingJob.id.desc())
        .limit(1)
    )
    alerts = db.scalars(
        select(FraudAlert)
        .where(FraudAlert.invoice_id == invoice.id)
        .order_by(FraudAlert.id.asc())
    ).all()
    cases = db.scalars(
        select(ReviewCase)
        .where(ReviewCase.invoice_id == invoice.id)
        .order_by(ReviewCase.id.asc())
    ).all()
    decisions = db.scalars(
        select(ReviewDecision)
        .join(ReviewCase, ReviewCase.id == ReviewDecision.case_id)
        .where(ReviewCase.invoice_id == invoice.id)
        .order_by(ReviewDecision.created_at.asc(), ReviewDecision.id.asc())
    ).all()
    snapshot = {
        "invoice": {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "vendor_id": invoice.vendor_id,
            "purchase_order_id": invoice.purchase_order_id,
            "currency": invoice.currency,
            "issue_date": _iso(invoice.issue_date),
            "due_date": _iso(invoice.due_date),
            "subtotal": _decimal(invoice.subtotal),
            "tax_amount": _decimal(invoice.tax_amount),
            "shipping_amount": _decimal(invoice.shipping_amount),
            "discount_amount": _decimal(invoice.discount_amount),
            "total_amount": _decimal(invoice.total_amount),
            "processing_status": invoice.processing_status,
            "workflow_status": invoice.workflow_status,
            "risk_score": invoice.heuristic_risk_score,
            "source": invoice.source,
            "updated_at": _iso(invoice.updated_at),
        },
        "vendor": {
            "id": vendor.id,
            "code": vendor.code,
            "name": vendor.name,
            "is_active": vendor.is_active,
            "risk_tier": vendor.risk_tier,
            "updated_at": _iso(vendor.updated_at),
        },
        "invoice_lines": [{
            "id": line.id,
            "line_number": line.line_number,
            "sku": line.sku,
            "description": line.description,
            "quantity": _decimal(line.quantity),
            "unit_price": _decimal(line.unit_price),
            "tax_rate": _decimal(line.tax_rate),
            "tax_amount": _decimal(line.tax_amount),
            "line_total": _decimal(line.line_total),
        } for line in lines],
        "documents": [{"id": doc.id, "sha256": doc.sha256, "byte_size": doc.byte_size} for doc in documents],
        "extraction": None if not extraction else {
            "id": extraction.id,
            "version": extraction.extraction_version,
            "engine_name": extraction.engine_name,
            "engine_version": extraction.engine_version,
            "extracted_at": _iso(extraction.extracted_at),
        },
        "match": {
            "id": match.id,
            "outcome": match.outcome,
            "details": match.details,
            "rule_version": match.rule_version,
            "evaluated_at": _iso(match.evaluated_at),
        },
        "job": None if not job else {
            "id": job.id,
            "state": job.state,
            "stage": job.stage,
            "attempts": job.attempts,
            "finished_at": _iso(job.finished_at),
        },
        "alerts": [{
            "id": alert.id,
            "alert_type": alert.alert_type,
            "severity": alert.severity,
            "status": alert.status,
            "resolved_at": _iso(alert.resolved_at),
        } for alert in alerts],
        "review_cases": [{
            "id": case.id,
            "status": case.status,
            "assigned_to_id": case.assigned_to_id,
            "updated_at": _iso(case.updated_at),
        } for case in cases],
        "review_decisions": [{
            "id": decision.id,
            "reviewer_id": decision.reviewer_id,
            "decision": decision.decision,
            "reason": decision.reason,
            "created_at": _iso(decision.created_at),
        } for decision in decisions],
        "approved_review_decision_id": review_decision.id if review_decision else None,
        "purchase_order": _purchase_order_snapshot(db, invoice),
    }
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _has_invoice_contribution(db: Session, invoice_id: str, actor_id: str) -> bool:
    if db.scalar(select(UploadReceipt.id).where(
        UploadReceipt.invoice_id == invoice_id,
        UploadReceipt.actor_id == actor_id,
    ).limit(1)):
        return True
    if db.scalar(select(AuditEvent.id).where(
        AuditEvent.object_type == "invoice",
        AuditEvent.object_id == invoice_id,
        AuditEvent.actor_id == actor_id,
        AuditEvent.action.in_(INVOICE_CONTRIBUTION_ACTIONS),
    ).limit(1)):
        return True
    if db.scalar(select(ReviewDecision.id).join(
        ReviewCase, ReviewCase.id == ReviewDecision.case_id,
    ).where(
        ReviewCase.invoice_id == invoice_id,
        ReviewDecision.reviewer_id == actor_id,
    ).limit(1)):
        return True
    if db.scalar(select(ReviewCase.id).where(
        ReviewCase.invoice_id == invoice_id,
        ReviewCase.assigned_to_id == actor_id,
    ).limit(1)):
        return True
    return False


def _assert_separate_actor(
    db: Session,
    proposal: PaymentProposal,
    invoice: Invoice,
    actor_id: str,
    *,
    action: str,
) -> None:
    if actor_id == proposal.created_by_id or actor_id == proposal.decided_by_id or _has_invoice_contribution(db, invoice.id, actor_id):
        raise HTTPException(
            status_code=403,
            detail=f"A separate person must {action}; invoice submission, review, correction, proposal creation, and proposal approval are separated.",
        )


def _assert_proposal_current(
    db: Session, proposal: PaymentProposal, invoice: Invoice,
) -> tuple[Vendor, InvoiceMatchResult, ReviewDecision | None]:
    vendor, match, review_decision = _validate_invoice_eligibility(db, invoice)
    decision_id = review_decision.id if review_decision else None
    if proposal.review_decision_id != decision_id:
        raise HTTPException(status_code=409, detail="The review decision changed; cancel and create a fresh proposal")
    if _invoice_fingerprint(db, invoice, vendor, match, review_decision) != proposal.invoice_fingerprint:
        raise HTTPException(status_code=409, detail="Payment-relevant invoice, vendor, match, or receipt data changed; cancel and create a fresh proposal")
    if (
        proposal.vendor_id != vendor.id
        or proposal.invoice_number != invoice.invoice_number
        or proposal.amount != invoice.total_amount
        or proposal.currency != invoice.currency.strip().upper()
        or proposal.vendor_code != vendor.code
        or proposal.vendor_name != vendor.name
        or proposal.due_date != invoice.due_date
    ):
        raise HTTPException(status_code=409, detail="The proposal snapshot no longer matches the invoice; cancel and create a fresh proposal")
    return vendor, match, review_decision


def _proposal_payload(proposal: PaymentProposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "invoice_id": proposal.invoice_id,
        "review_decision_id": proposal.review_decision_id,
        "version": proposal.version,
        "invoice_number": proposal.invoice_number,
        "vendor_id": proposal.vendor_id,
        "vendor_code": proposal.vendor_code,
        "vendor_name": proposal.vendor_name,
        "amount": _decimal(proposal.amount),
        "currency": proposal.currency,
        "due_date": _iso(proposal.due_date),
        "status": proposal.status,
        "proposal_reason": proposal.proposal_reason,
        "created_by_id": proposal.created_by_id,
        "created_at": _iso(proposal.created_at),
        "decided_by_id": proposal.decided_by_id,
        "resolution_reason": proposal.resolution_reason,
        "decided_at": _iso(proposal.decided_at),
        "handoff_count": proposal.handoff_count,
        "last_handoff_at": _iso(proposal.last_handoff_at),
        "last_handoff_by_id": proposal.last_handoff_by_id,
        "payment_executed": False,
        "executable": False,
        "payment_rail_configured": False,
    }


def _safe_reason(value: str) -> str:
    reason = value.strip()
    if len(reason) < 10:
        raise HTTPException(status_code=422, detail="A reason of at least 10 non-whitespace characters is required")
    return reason


@router.post("", status_code=201)
def create_payment_proposal(
    payload: PaymentProposalCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "ap_clerk")),
):
    invoice = db.scalar(select(Invoice).where(Invoice.id == payload.invoice_id).with_for_update())
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    reason = _safe_reason(payload.reason)
    vendor, match, review_decision = _validate_invoice_eligibility(db, invoice)
    active = db.scalar(
        select(PaymentProposal.id)
        .where(
            PaymentProposal.invoice_id == invoice.id,
            PaymentProposal.status.in_(("pending_approval", "approved")),
        )
        .limit(1)
    )
    if active:
        raise HTTPException(status_code=409, detail="An active proposal already exists for this invoice")
    version = int(db.scalar(
        select(func.max(PaymentProposal.version)).where(PaymentProposal.invoice_id == invoice.id)
    ) or 0) + 1
    proposal = PaymentProposal(
        invoice_id=invoice.id,
        review_decision_id=review_decision.id if review_decision else None,
        vendor_id=vendor.id,
        version=version,
        amount=invoice.total_amount,
        currency=invoice.currency.strip().upper(),
        invoice_number=invoice.invoice_number,
        vendor_code=vendor.code,
        vendor_name=vendor.name,
        due_date=invoice.due_date,
        invoice_fingerprint=_invoice_fingerprint(db, invoice, vendor, match, review_decision),
        proposal_reason=reason,
        status="pending_approval",
        created_by_id=user.id,
    )
    db.add(proposal)
    try:
        db.flush()
        append_audit_event(
            db,
            actor_id=user.id,
            action="payment_proposal.created",
            object_type="payment_proposal",
            object_id=proposal.id,
            details={
                "invoice_id": invoice.id,
                "version": version,
                "amount": _decimal(proposal.amount),
                "currency": proposal.currency,
                "status": proposal.status,
                "executable": False,
                "payment_executed": False,
            },
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A concurrent proposal was created; refresh and review the current proposal") from None
    return _proposal_payload(proposal)


@router.get("")
def list_payment_proposals(
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    invoice_id: str | None = Query(default=None, min_length=1, max_length=36),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles(*READ_ROLES)),
):
    allowed_statuses = {"pending_approval", "approved", "rejected", "cancelled"}
    if status_filter and status_filter not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Unknown payment proposal status")
    query = select(PaymentProposal)
    if status_filter:
        query = query.where(PaymentProposal.status == status_filter)
    if invoice_id:
        query = query.where(PaymentProposal.invoice_id == invoice_id)
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    proposals = db.scalars(
        query.order_by(PaymentProposal.created_at.desc(), PaymentProposal.version.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [_proposal_payload(item) for item in proposals],
        "total": total,
        "limit": limit,
        "offset": offset,
        "payment_execution_enabled": False,
    }


@router.post("/handoff")
def export_non_executable_handoff_packet(
    payload: PaymentProposalHandoffRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("ap_clerk")),
):
    reason = _safe_reason(payload.reason)
    if len(payload.proposal_ids) != len(set(payload.proposal_ids)):
        raise HTTPException(status_code=422, detail="Duplicate proposal IDs are not allowed")
    proposals = db.scalars(
        select(PaymentProposal)
        .where(PaymentProposal.id.in_(payload.proposal_ids))
        .order_by(PaymentProposal.id.asc())
        .with_for_update()
    ).all()
    if len(proposals) != len(payload.proposal_ids):
        raise HTTPException(status_code=404, detail="One or more payment proposals were not found")

    now = datetime.now(timezone.utc)
    packet_items: list[dict[str, Any]] = []
    for proposal in proposals:
        if proposal.status != "approved":
            raise HTTPException(status_code=409, detail="Only independently approved proposals can be included in a handoff packet")
        invoice = db.get(Invoice, proposal.invoice_id)
        if not invoice:
            raise HTTPException(status_code=409, detail="The source invoice is unavailable; handoff was not created")
        vendor, _match, _review = _assert_proposal_current(db, proposal, invoice)
        _assert_separate_actor(db, proposal, invoice, user.id, action="export the human handoff packet")
        proposal.handoff_count += 1
        proposal.last_handoff_at = now
        proposal.last_handoff_by_id = user.id
        append_audit_event(
            db,
            actor_id=user.id,
            action="payment_proposal.handoff_exported",
            object_type="payment_proposal",
            object_id=proposal.id,
            details={
                "invoice_id": invoice.id,
                "version": proposal.version,
                "batch_size": len(proposals),
                "reason": reason,
                "destination": "local human-controlled process only",
                "external_network_call": False,
                "executable": False,
                "payment_executed": False,
            },
        )
        packet_items.append({
            "proposal_id": proposal.id,
            "proposal_version": proposal.version,
            "invoice_id": invoice.id,
            "invoice_number": proposal.invoice_number,
            "vendor_id": vendor.id,
            "vendor_code": vendor.code,
            "vendor_name": vendor.name,
            "amount": _decimal(proposal.amount),
            "currency": proposal.currency,
            "due_date": _iso(proposal.due_date),
            "proposal_reason": proposal.proposal_reason,
            "approval": {
                "status": proposal.status,
                "approved_by_id": proposal.decided_by_id,
                "approved_at": _iso(proposal.decided_at),
                "reason": proposal.resolution_reason,
            },
            "payment_executed": False,
            "executable": False,
        })
    db.commit()
    body = {
        "packet_type": "non_executable_human_review_handoff",
        "generated_at": now.isoformat(),
        "reason": reason,
        "items": packet_items,
        "not_payment_instruction": True,
        "payment_execution_enabled": False,
        "payment_executed": False,
        "executable": False,
        "external_network_call": False,
        "disclaimer": "This local review packet is not a payment instruction and cannot initiate, schedule, or execute a payment. No bank details or payment-rail data are included.",
    }
    return JSONResponse(
        content=body,
        headers={"Content-Disposition": 'attachment; filename="invoiceguard-non-executable-proposals.json"'},
    )


@router.get("/{proposal_id}")
def get_payment_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles(*READ_ROLES)),
):
    proposal = db.get(PaymentProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Payment proposal not found")
    return _proposal_payload(proposal)


@router.post("/{proposal_id}/decision")
def decide_payment_proposal(
    proposal_id: str,
    payload: PaymentProposalDecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("approver")),
):
    proposal = db.scalar(
        select(PaymentProposal)
        .where(PaymentProposal.id == proposal_id)
        .with_for_update()
    )
    if not proposal:
        raise HTTPException(status_code=404, detail="Payment proposal not found")
    if proposal.status != "pending_approval":
        raise HTTPException(status_code=409, detail=f"Proposal is already {proposal.status}")
    invoice = db.get(Invoice, proposal.invoice_id)
    if not invoice:
        raise HTTPException(status_code=409, detail="The source invoice is unavailable")
    _assert_proposal_current(db, proposal, invoice)
    _assert_separate_actor(db, proposal, invoice, user.id, action="approve or reject this proposal")
    reason = _safe_reason(payload.reason)
    proposal.status = "approved" if payload.decision == "approve" else "rejected"
    proposal.decided_by_id = user.id
    proposal.resolution_reason = reason
    proposal.decided_at = datetime.now(timezone.utc)
    append_audit_event(
        db,
        actor_id=user.id,
        action="payment_proposal.decided",
        object_type="payment_proposal",
        object_id=proposal.id,
        details={
            "invoice_id": invoice.id,
            "decision": payload.decision,
            "status": proposal.status,
            "reason": reason,
            "separation_of_duties_checked": True,
            "executable": False,
            "payment_executed": False,
        },
    )
    db.commit()
    return {
        "proposal": _proposal_payload(proposal),
        "payment_executed": False,
        "executable": False,
        "next_step": "A separate human-controlled process may review the non-executable packet; no payment is created or sent by this API.",
    }


@router.post("/{proposal_id}/cancel")
def cancel_payment_proposal(
    proposal_id: str,
    payload: PaymentProposalCancelRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "ap_clerk")),
):
    proposal = db.scalar(
        select(PaymentProposal)
        .where(PaymentProposal.id == proposal_id)
        .with_for_update()
    )
    if not proposal:
        raise HTTPException(status_code=404, detail="Payment proposal not found")
    if proposal.status not in {"pending_approval", "approved"}:
        raise HTTPException(status_code=409, detail=f"Proposal is already {proposal.status}")
    if proposal.created_by_id != user.id and "admin" not in {role.name for role in user.roles}:
        raise HTTPException(status_code=403, detail="Only the proposal creator or an administrator can cancel this proposal")
    if proposal.handoff_count > 0:
        raise HTTPException(status_code=409, detail="A previously exported packet cannot be recalled by this application")
    reason = _safe_reason(payload.reason)
    proposal.status = "cancelled"
    proposal.decided_by_id = user.id
    proposal.resolution_reason = reason
    proposal.decided_at = datetime.now(timezone.utc)
    append_audit_event(
        db,
        actor_id=user.id,
        action="payment_proposal.cancelled",
        object_type="payment_proposal",
        object_id=proposal.id,
        details={
            "invoice_id": proposal.invoice_id,
            "version": proposal.version,
            "reason": reason,
            "executable": False,
            "payment_executed": False,
        },
    )
    db.commit()
    return {"proposal": _proposal_payload(proposal), "payment_executed": False, "executable": False}
