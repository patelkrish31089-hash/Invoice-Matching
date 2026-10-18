from __future__ import annotations

import json
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.common import case_payload, case_payloads
from app.audit import append_audit_event
from app.db import get_db, requires_write_transaction
from app.models import FraudAlert, Invoice, ModelFeedback, ReviewCase, ReviewDecision, User
from app.schemas import ReviewAssignmentRequest, ReviewDecisionRequest
from app.security import get_current_user, require_roles

router = APIRouter(prefix="/review", tags=["human review"])
OPEN_STATUSES = ["open", "in_review", "waiting_documents", "escalated"]


@router.get("/cases")
def list_review_cases(
    status_filter: str | None = Query(default=None, alias="status", max_length=30),
    priority: str | None = Query(default=None, max_length=20),
    assigned_to_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "ap_clerk", "reviewer", "auditor")),
):
    query = select(ReviewCase)
    if status_filter:
        query = query.where(ReviewCase.status == status_filter)
    else:
        query = query.where(ReviewCase.status.in_(OPEN_STATUSES))
    if priority:
        query = query.where(ReviewCase.priority == priority)
    if assigned_to_id:
        query = query.where(ReviewCase.assigned_to_id == assigned_to_id)
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    priority_order = case(
        (ReviewCase.priority == "critical", 0),
        (ReviewCase.priority == "high", 1),
        (ReviewCase.priority == "normal", 2),
        (ReviewCase.priority == "low", 3),
        else_=4,
    )
    cases = db.scalars(query.order_by(priority_order, ReviewCase.created_at.asc()).offset(offset).limit(limit)).all()
    return {"items": case_payloads(db, cases), "total": total, "limit": limit, "offset": offset}


@router.get("/cases/{case_id}")
def get_review_case(case_id: str, db: Session = Depends(get_db), _user: User = Depends(require_roles("admin", "ap_clerk", "reviewer", "auditor"))):
    case = db.get(ReviewCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Review case not found")
    return case_payload(db, case, include_detail=True)


@router.post("/cases/{case_id}/assign")
def assign_review_case(
    case_id: str, payload: ReviewAssignmentRequest,
    db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "reviewer")),
):
    case = db.get(ReviewCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Review case not found")
    assignee = db.get(User, payload.assigned_to_id)
    if not assignee or not assignee.is_active:
        raise HTTPException(status_code=404, detail="Active assignee not found")
    role_names = {role.name for role in assignee.roles}
    if not role_names.intersection({"admin", "reviewer", "ap_clerk"}):
        raise HTTPException(status_code=422, detail="Assignee must have a review-capable role")
    previous = case.assigned_to_id
    case.assigned_to_id = assignee.id
    if case.status == "open":
        case.status = "in_review"
    append_audit_event(db, actor_id=user.id, action="review.case_assigned", object_type="review_case", object_id=case.id, details={"previous_assignee": previous, "assigned_to": assignee.id})
    db.commit()
    return case_payload(db, case)


@router.post("/cases/{case_id}/decision")
def decide_review_case(
    case_id: str, payload: ReviewDecisionRequest,
    db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "reviewer")),
):
    case = db.get(ReviewCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Review case not found")
    if case.status not in OPEN_STATUSES:
        raise HTTPException(status_code=409, detail=f"Case is already {case.status}")
    invoice = db.get(Invoice, case.invoice_id)
    if payload.decision == "approved" and (invoice.heuristic_risk_score or 0) >= 0.85:
        role_names = {role.name for role in user.roles}
        if "admin" not in role_names or len(payload.reason.strip()) < 20:
            raise HTTPException(status_code=403, detail="Critical-risk approval requires an admin role and a reason of at least 20 characters")
    if len(json.dumps(payload.evidence, default=str)) > 12_000:
        raise HTTPException(status_code=413, detail="Decision evidence must be smaller than 12 KB")
    decision = ReviewDecision(case_id=case.id, reviewer_id=user.id, decision=payload.decision, reason=payload.reason.strip(), evidence=payload.evidence)
    db.add(decision)
    if payload.decision == "approved":
        case.status = "approved"
        invoice.workflow_status = "approved_by_reviewer"
    elif payload.decision == "rejected":
        case.status = "rejected"
        invoice.workflow_status = "rejected_by_reviewer"
    elif payload.decision == "escalated":
        case.status = "escalated"
        invoice.workflow_status = "escalated"
    elif payload.decision == "request_documents":
        case.status = "waiting_documents"
        case.requested_documents = payload.reason.strip()
        invoice.workflow_status = "waiting_documents"
    elif payload.decision == "confirmed_suspicious":
        case.status = "confirmed_suspicious"
        invoice.workflow_status = "suspicious_confirmed"
        for alert in db.scalars(select(FraudAlert).where(FraudAlert.invoice_id == invoice.id, FraudAlert.status == "open")):
            alert.status = "confirmed_suspicious"
            alert.resolved_at = datetime.now(timezone.utc)
    elif payload.decision == "false_positive":
        case.status = "false_positive"
        invoice.workflow_status = "reviewed_false_positive"
        for alert in db.scalars(select(FraudAlert).where(FraudAlert.invoice_id == invoice.id, FraudAlert.status == "open")):
            alert.status = "false_positive"
            alert.resolved_at = datetime.now(timezone.utc)
    append_audit_event(
        db, actor_id=user.id, action="review.decision_recorded", object_type="review_case", object_id=case.id,
        details={"decision": payload.decision, "reason": payload.reason.strip(), "invoice_id": invoice.id},
    )
    db.commit()
    return {"case": case_payload(db, case, include_detail=True), "payment_executed": False, "feedback_status": "captured_for_controlled_review_not_automatic_retraining"}


@router.post("/cases/{case_id}/feedback/approve")
def approve_feedback_for_training(
    case_id: str,
    feedback_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    case = db.get(ReviewCase, case_id)
    feedback = db.get(ModelFeedback, feedback_id)
    if not case or not feedback or feedback.invoice_id != case.invoice_id:
        raise HTTPException(status_code=404, detail="Case feedback not found")
    if case.status not in {"approved", "rejected", "confirmed_suspicious", "false_positive"}:
        raise HTTPException(status_code=409, detail="Feedback can only be promoted after a resolved human decision")
    feedback.approved_for_training = True
    append_audit_event(db, actor_id=user.id, action="ml.feedback_approved_for_training", object_type="model_feedback", object_id=feedback.id, details={"case_id": case.id, "decision_status": case.status})
    db.commit()
    return {"feedback_id": feedback.id, "approved_for_training": True, "automatic_retraining": False}


@router.get("/feedback/training-export")
@requires_write_transaction
def export_approved_feedback(
    limit: int = Query(default=500, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """Download only explicitly approved correction pairs; this endpoint never trains a model."""
    query = (
        select(ModelFeedback)
        .where(ModelFeedback.approved_for_training.is_(True))
        .order_by(ModelFeedback.created_at.asc(), ModelFeedback.id.asc())
        .offset(offset)
        .limit(limit)
    )
    total_approved = int(db.scalar(
        select(func.count()).select_from(ModelFeedback).where(ModelFeedback.approved_for_training.is_(True))
    ) or 0)
    feedback_rows = db.scalars(query).all()
    output: list[bytes] = []
    skipped = 0
    for feedback in feedback_rows:
        payload = feedback.correction_payload if isinstance(feedback.correction_payload, dict) else {}
        previous = payload.get("previous")
        corrected = payload.get("corrected")
        if not isinstance(previous, dict) or not isinstance(corrected, dict) or not corrected:
            skipped += 1
            continue
        record = {
            "schema_version": "invoice-correction-jsonl-v1",
            "sample_id": feedback.id,
            "invoice_id": feedback.invoice_id,
            "reviewed_at": feedback.created_at.isoformat(),
            "prediction": previous,
            "label": corrected,
            "label_source": "human_correction_approved_after_resolved_review",
        }
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(serialized.encode("utf-8")) > 64_000:
            skipped += 1
            continue
        output.append((serialized + "\n").encode("utf-8"))
    export_id = str(uuid.uuid4())
    append_audit_event(
        db,
        actor_id=user.id,
        action="ml.approved_feedback_exported",
        object_type="model_feedback_export",
        object_id=export_id,
        details={"returned": len(output), "approved_total": total_approved, "skipped_invalid_or_oversize": skipped, "limit": limit, "offset": offset},
    )
    db.commit()
    return StreamingResponse(
        iter(output),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": "attachment; filename=approved-invoice-feedback.jsonl",
            "Cache-Control": "no-store",
            "X-Feedback-Export-ID": export_id,
            "X-Feedback-Export-Total": str(total_approved),
            "X-Feedback-Export-Offset": str(offset),
            "X-Feedback-Export-Returned": str(len(output)),
            "X-Feedback-Export-Skipped": str(skipped),
        },
    )
