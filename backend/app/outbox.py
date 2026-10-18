from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.config import settings
from app.models import Invoice, ProcessingJob, ProcessingOutbox, ReviewCase

logger = logging.getLogger("ledgerlens.outbox")

OUTBOX_LOCK_SECONDS = 120
OUTBOX_MAX_ATTEMPTS = 8
OUTBOX_BASE_BACKOFF_SECONDS = 2
OUTBOX_MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class OutboxClaim:
    event_id: str
    job_id: str
    actor_id: str | None
    claim_token: str
    attempts: int


def enqueue_processing_event(
    db: Session, *, job_id: str, invoice_id: str, actor_id: str | None,
) -> ProcessingOutbox:
    """Stage one unique dispatch event in the caller's invoice/job transaction."""
    event = ProcessingOutbox(
        job_id=job_id, invoice_id=invoice_id, actor_id=actor_id,
        event_type="process_invoice", state="pending", attempts=0,
        available_at=datetime.now(timezone.utc),
    )
    db.add(event)
    return event


def _claim_one(
    session_factory, *, event_id: str | None = None, force: bool = False,
) -> OutboxClaim | None:
    now = datetime.now(timezone.utc)
    eligible = [
        ProcessingOutbox.state == "pending",
        ProcessingOutbox.attempts < OUTBOX_MAX_ATTEMPTS,
        or_(ProcessingOutbox.locked_until.is_(None), ProcessingOutbox.locked_until <= now),
    ]
    if not force:
        eligible.append(ProcessingOutbox.available_at <= now)
    if event_id:
        eligible.append(ProcessingOutbox.id == event_id)
    with session_factory() as db:
        candidate = db.scalar(
            select(ProcessingOutbox)
            .where(*eligible)
            .order_by(ProcessingOutbox.created_at, ProcessingOutbox.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not candidate:
            db.rollback()
            return None
        token = str(uuid.uuid4())
        claimed_attempt = int(candidate.attempts or 0) + 1
        result = db.execute(
            update(ProcessingOutbox)
            .where(ProcessingOutbox.id == candidate.id, *eligible)
            .values(
                attempts=ProcessingOutbox.attempts + 1,
                locked_until=now + timedelta(seconds=OUTBOX_LOCK_SECONDS),
                claim_token=token,
            ).execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        claim = OutboxClaim(
            event_id=candidate.id, job_id=candidate.job_id,
            actor_id=candidate.actor_id, claim_token=token, attempts=claimed_attempt,
        )
        db.commit()
        return claim


def _finish_event(session_factory, claim: OutboxClaim, *, state: str, error_type: str | None = None) -> bool:
    with session_factory() as db:
        result = db.execute(
            update(ProcessingOutbox)
            .where(
                ProcessingOutbox.id == claim.event_id,
                ProcessingOutbox.state == "pending",
                ProcessingOutbox.claim_token == claim.claim_token,
            )
            .values(
                state=state,
                published_at=datetime.now(timezone.utc) if state == "published" else None,
                locked_until=None, claim_token=None, last_error_type=error_type,
            )
        )
        db.commit()
        return result.rowcount == 1


def _fail_job_after_dispatch_exhaustion(session_factory, claim: OutboxClaim, error_type: str) -> None:
    """Mark a job failed only after bounded outbox retries; keep error details redacted."""
    now = datetime.now(timezone.utc)
    safe_message = f"Worker dispatch failed after {claim.attempts} attempts ({error_type}). The document is saved; an administrator can retry processing."
    with session_factory() as db:
        event = db.get(ProcessingOutbox, claim.event_id)
        if not event or event.claim_token != claim.claim_token or event.state != "pending":
            db.rollback()
            return
        job = db.get(ProcessingJob, claim.job_id)
        if not job:
            event.state = "dead"
            event.locked_until = None
            event.claim_token = None
            event.last_error_type = error_type
            db.commit()
            return
        if job.state != "queued":
            # The task may have been accepted even if publishing's acknowledgement was lost.
            event.state = "published"
            event.published_at = now
            event.locked_until = None
            event.claim_token = None
            db.commit()
            return
        job.state = "failed"
        job.stage = "dispatch"
        job.error_message = safe_message
        job.finished_at = now
        job.heartbeat_at = now
        invoice = db.get(Invoice, job.invoice_id)
        if invoice:
            invoice.processing_status = "failed"
            invoice.workflow_status = "review_required"
            open_case = db.scalar(
                select(ReviewCase)
                .where(
                    ReviewCase.invoice_id == invoice.id,
                    ReviewCase.status.in_(["open", "in_review", "waiting_documents", "escalated"]),
                )
                .order_by(ReviewCase.created_at.desc())
                .limit(1)
            )
            if open_case:
                open_case.reason_codes = sorted(set(open_case.reason_codes or []).union({"processing_dispatch_failure"}))
                open_case.summary = "The invoice is saved, but its processing message exhausted bounded dispatch retries. An admin must retry processing after checking the queue."
            else:
                db.add(ReviewCase(
                    invoice_id=invoice.id, status="open", priority="normal",
                    reason_codes=["processing_dispatch_failure"],
                    summary="The invoice is saved, but its processing message exhausted bounded dispatch retries. An admin must retry processing after checking the queue.",
                ))
            append_audit_event(
                db, actor_id=claim.actor_id, action="invoice.processing_dispatch_exhausted",
                object_type="processing_job", object_id=job.id,
                details={"invoice_id": invoice.id, "attempts": claim.attempts, "error_type": error_type},
            )
        event.state = "dead"
        event.locked_until = None
        event.claim_token = None
        event.last_error_type = error_type
        db.commit()


def _finalize_expired_exhausted_events(
    session_factory, *, event_id: str | None = None, limit: int = 100,
) -> int:
    """Finalize claims that crashed after consuming the configured attempt budget."""
    now = datetime.now(timezone.utc)
    eligible = [
        ProcessingOutbox.state == "pending",
        ProcessingOutbox.attempts >= OUTBOX_MAX_ATTEMPTS,
        or_(ProcessingOutbox.locked_until.is_(None), ProcessingOutbox.locked_until <= now),
    ]
    if event_id:
        eligible.append(ProcessingOutbox.id == event_id)
    claims: list[OutboxClaim] = []
    with session_factory() as db:
        candidates = db.scalars(
            select(ProcessingOutbox).where(*eligible)
            .order_by(ProcessingOutbox.created_at, ProcessingOutbox.id)
            .with_for_update(skip_locked=True).limit(limit)
        ).all()
        for event in candidates:
            token = str(uuid.uuid4())
            result = db.execute(
                update(ProcessingOutbox)
                .where(ProcessingOutbox.id == event.id, *eligible)
                .values(claim_token=token, locked_until=now + timedelta(seconds=OUTBOX_LOCK_SECONDS))
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 1:
                claims.append(OutboxClaim(
                    event_id=event.id, job_id=event.job_id, actor_id=event.actor_id,
                    claim_token=token, attempts=int(event.attempts or OUTBOX_MAX_ATTEMPTS),
                ))
        db.commit()
    for claim in claims:
        with session_factory() as db:
            event = db.get(ProcessingOutbox, claim.event_id)
            safe_error_type = (event.last_error_type if event else None) or "DispatchOutcomeUnknown"
        _fail_job_after_dispatch_exhaustion(session_factory, claim, safe_error_type)
    return len(claims)


def _defer_failure(session_factory, claim: OutboxClaim, error_type: str) -> None:
    if claim.attempts >= OUTBOX_MAX_ATTEMPTS:
        _fail_job_after_dispatch_exhaustion(session_factory, claim, error_type)
        logger.error(
            "outbox_dispatch_exhausted event_id=%s job_id=%s attempts=%s error_type=%s",
            claim.event_id, claim.job_id, claim.attempts, error_type,
        )
        return
    delay = min(OUTBOX_MAX_BACKOFF_SECONDS, OUTBOX_BASE_BACKOFF_SECONDS * (2 ** max(0, claim.attempts - 1)))
    with session_factory() as db:
        db.execute(
            update(ProcessingOutbox)
            .where(
                ProcessingOutbox.id == claim.event_id,
                ProcessingOutbox.state == "pending",
                ProcessingOutbox.claim_token == claim.claim_token,
            )
            .values(
                available_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
                locked_until=None, claim_token=None, last_error_type=error_type,
            )
        )
        db.commit()
    logger.warning(
        "outbox_dispatch_deferred event_id=%s job_id=%s attempt=%s retry_seconds=%s error_type=%s",
        claim.event_id, claim.job_id, claim.attempts, delay, error_type,
    )


def dispatch_outbox_event(
    session_factory, storage_dir: Path, event_id: str, *, force: bool = False,
) -> str | None:
    """Claim and dispatch one event. Returns a safe exception class name, never its message."""
    _finalize_expired_exhausted_events(session_factory, event_id=event_id, limit=1)
    claim = _claim_one(session_factory, event_id=event_id, force=force)
    if not claim:
        return None
    with session_factory() as db:
        job = db.get(ProcessingJob, claim.job_id)
        job_state = job.state if job else None
    if job_state != "queued":
        _finish_event(session_factory, claim, state="published")
        return None
    try:
        if settings.task_mode.lower() == "celery":
            from app.tasks import process_invoice_task
            process_invoice_task.delay(claim.job_id)
        else:
            from app.processing import run_processing_job
            run_processing_job(claim.job_id, session_factory, storage_dir)
    except Exception as exc:
        error_type = type(exc).__name__[:80]
        _defer_failure(session_factory, claim, error_type)
        return error_type
    _finish_event(session_factory, claim, state="published")
    return None


def dispatch_outbox_for_job(session_factory, storage_dir: Path, job_id: str, *, force: bool = True) -> str | None:
    with session_factory() as db:
        event_id = db.scalar(
            select(ProcessingOutbox.id)
            .where(ProcessingOutbox.job_id == job_id, ProcessingOutbox.event_type == "process_invoice")
            .limit(1)
        )
        db.rollback()
    if not event_id:
        # Compatibility/recovery for jobs created before the outbox migration or by an older process.
        with session_factory() as db:
            job = db.get(ProcessingJob, job_id)
            if not job:
                db.rollback()
                return None
            event = enqueue_processing_event(db, job_id=job.id, invoice_id=job.invoice_id, actor_id=None)
            db.commit()
            event_id = event.id
    return dispatch_outbox_event(session_factory, storage_dir, event_id, force=force)


def dispatch_pending_outbox(session_factory, storage_dir: Path, *, limit: int = 25) -> dict[str, int]:
    """Dispatch a bounded batch of due events; safe for multiple competing dispatchers."""
    if limit < 1 or limit > 500:
        raise ValueError("Outbox batch limit must be between 1 and 500")
    summary = {"claimed": 0, "published": 0, "deferred_or_dead": 0}
    summary["deferred_or_dead"] += _finalize_expired_exhausted_events(session_factory, limit=limit)
    for _ in range(limit):
        claim = _claim_one(session_factory)
        if not claim:
            break
        summary["claimed"] += 1
        with session_factory() as db:
            job = db.get(ProcessingJob, claim.job_id)
            job_state = job.state if job else None
        if job_state != "queued":
            _finish_event(session_factory, claim, state="published")
            summary["published"] += 1
            continue
        try:
            if settings.task_mode.lower() == "celery":
                from app.tasks import process_invoice_task
                process_invoice_task.delay(claim.job_id)
            else:
                from app.processing import run_processing_job
                run_processing_job(claim.job_id, session_factory, storage_dir)
        except Exception as exc:
            _defer_failure(session_factory, claim, type(exc).__name__[:80])
            summary["deferred_or_dead"] += 1
        else:
            _finish_event(session_factory, claim, state="published")
            summary["published"] += 1
    return summary


def outbox_event_payload(event: ProcessingOutbox) -> dict[str, Any]:
    return {
        "id": event.id, "invoice_id": event.invoice_id, "job_id": event.job_id,
        "event_type": event.event_type, "state": event.state, "attempts": event.attempts,
        "available_at": event.available_at.isoformat() if event.available_at else None,
        "published_at": event.published_at.isoformat() if event.published_at else None,
        "last_error_type": event.last_error_type,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
