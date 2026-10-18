from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import mimetypes
import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.common import invoice_summary, invoice_summaries, latest_assessment, latest_extraction, latest_match
from app.audit import append_audit_event
from app.config import settings
from app.db import get_db
from app.outbox import dispatch_outbox_event, enqueue_processing_event
from app.models import (
    DocumentFile,
    ExtractionResult,
    IdempotencyRecord,
    Invoice,
    InvoiceLine,
    InvoiceMatchResult,
    ProcessingJob,
    ProcessingOutbox,
    PurchaseOrder,
    UploadReceipt,
    User,
    Vendor,
)
from app.processing import _resolve_vendor, evaluate_invoice
from app.schemas import InvoiceCorrectionRequest, UploadOperationStatusResponse
from app.security import require_roles
from app.services.document_processing import render_document_page
from app.services.docx_processing import DOCX_MIME_TYPE, DocxLimitError, DocxProcessingError, validate_docx
from app.storage import promote_upload_staging_file, write_upload_staging_file
from app.upload_recovery import abandon_upload_receipt
from app.services.malware_quarantine import record_quarantined_upload, scan_submission
from app.services.malware_scan import MalwareScannerError, MalwareScanSizeError

router = APIRouter(prefix="/invoices", tags=["invoices"])

ALLOWED_SUFFIXES = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
ALLOWED_FIELDS = {
    "vendor_name", "vendor_tax_id", "invoice_number", "purchase_order_number",
    "issue_date", "due_date", "currency", "subtotal", "tax_amount",
    "shipping_amount", "discount_amount", "total_amount", "line_items",
}
READ_ROLES = ("admin", "ap_clerk", "approver", "reviewer", "auditor")
WRITE_ROLES = ("admin", "ap_clerk", "reviewer")


def _verify_document(data: bytes, filename: str) -> tuple[str, str]:
    """Validate extension and magic bytes; MIME headers from clients are not trusted."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=415, detail="Only PDF, DOCX, PNG, JPG/JPEG, TIFF, and BMP files are supported")
    if suffix == ".docx":
        try:
            validate_docx(data)
        except DocxLimitError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from None
        except DocxProcessingError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from None
        return DOCX_MIME_TYPE, suffix
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise HTTPException(status_code=415, detail="The uploaded file does not have a valid PDF signature")
        return "application/pdf", suffix
    signatures: dict[str, tuple[bytes, ...]] = {
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".tif": (b"II*\x00", b"MM\x00*"),
        ".tiff": (b"II*\x00", b"MM\x00*"),
        ".bmp": (b"BM",),
    }
    if not any(data.startswith(signature) for signature in signatures[suffix]):
        raise HTTPException(status_code=415, detail="The file extension does not match the file signature")
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream", suffix


def _upload_receipt_key_hash(
    actor_id: str,
    source: str,
    vendor_id: str | None,
    idempotency_key: str | None,
) -> str | None:
    """Hash the operation key in the same actor/source/vendor scope used at reservation."""
    if not idempotency_key:
        return None
    if source == "upload" and vendor_id is None:
        key_material = f"{actor_id}:{idempotency_key}"
    else:
        key_material = f"{actor_id}:{source}:{vendor_id or ''}:{idempotency_key}"
    return sha256(key_material.encode("utf-8")).hexdigest()


def _dispatch_or_record_failure(
    db: Session, request: Request, background_tasks: BackgroundTasks, *,
    invoice_id: str, job_id: str, actor_id: str, force: bool = True,
) -> str | None:
    """Dispatch through the durable outbox; a broker error leaves a retryable event."""
    event = db.scalar(
        select(ProcessingOutbox)
        .where(ProcessingOutbox.job_id == job_id, ProcessingOutbox.event_type == "process_invoice")
        .limit(1)
    )
    if not event:
        job = db.get(ProcessingJob, job_id)
        if not job:
            return "MissingProcessingJob"
        event = enqueue_processing_event(db, job_id=job.id, invoice_id=invoice_id, actor_id=actor_id)
        db.commit()
    event_id = event.id
    db.rollback()  # Release the read transaction before the independent dispatch session updates the outbox.
    if settings.task_mode.lower() == "celery":
        return dispatch_outbox_event(
            request.app.state.session_factory, request.app.state.storage_dir, event_id, force=force,
        )
    background_tasks.add_task(
        dispatch_outbox_event, request.app.state.session_factory,
        request.app.state.storage_dir, event_id, force=force,
    )
    return None


def _upload_response_for_current_job(
    response: dict, invoice: Invoice, job: ProcessingJob, outbox: ProcessingOutbox | None = None,
) -> dict:
    updated = dict(response)
    updated.update({
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "job_id": job.id,
        "job_state": job.state,
        "job_stage": job.stage,
        "processing_status": invoice.processing_status,
        "workflow_status": invoice.workflow_status,
    })
    if outbox:
        updated["dispatch_state"] = outbox.state
        updated["dispatch_attempts"] = outbox.attempts
        updated["dispatch_next_attempt_at"] = outbox.available_at.isoformat() if outbox.available_at else None
        updated["dispatch_error_type"] = outbox.last_error_type
        if outbox.state == "dead":
            updated["dispatch_error"] = "The processing message exhausted retries. An administrator must retry it after checking worker health."
            updated["message"] = "The document is saved, but processing dispatch needs administrator attention. No payment was attempted."
        elif outbox.state == "pending" and outbox.last_error_type:
            updated["dispatch_warning"] = "The document is saved and the processing job is queued; the dispatcher will retry."
    return updated


def _replay_upload_response(
    db: Session, record: IdempotencyRecord, request: Request, background_tasks: BackgroundTasks,
) -> dict:
    response = dict(record.response_body or {})
    invoice_id, job_id = response.get("invoice_id"), response.get("job_id")
    invoice = db.get(Invoice, invoice_id) if invoice_id else None
    job = db.get(ProcessingJob, job_id) if job_id else None
    if not invoice or not job:
        raise HTTPException(
            status_code=409,
            detail="The prior upload record is incomplete; review the existing invoice before using another key.",
        )
    outbox = db.scalar(
        select(ProcessingOutbox)
        .where(ProcessingOutbox.job_id == job.id, ProcessingOutbox.event_type == "process_invoice")
        .limit(1)
    )
    if job.state == "failed" and job.stage == "dispatch":
        # Same actor + key + identical content is a safe retry of the same durable operation.
        if not outbox:
            outbox = enqueue_processing_event(db, job_id=job.id, invoice_id=invoice.id, actor_id=record.actor_id)
        if outbox.state in {"dead", "pending"}:
            now = datetime.now(timezone.utc)
            job.state, job.stage, job.error_message = "queued", "queued", None
            job.started_at = None
            job.finished_at = None
            job.heartbeat_at = None
            invoice.processing_status, invoice.workflow_status = "queued", "processing"
            outbox.state, outbox.attempts = "pending", 0
            outbox.available_at, outbox.locked_until, outbox.claim_token = now, None, None
            outbox.published_at, outbox.last_error_type = None, None
            append_audit_event(
                db, actor_id=record.actor_id, action="invoice.processing_dispatch_retried",
                object_type="processing_job", object_id=job.id,
                details={"invoice_id": invoice.id, "source": "actor_bound_idempotency_replay"},
            )
            db.commit()
            db.refresh(invoice)
            db.refresh(job)
            db.refresh(outbox)
    dispatch_error = None
    if job.state == "queued":
        dispatch_error = _dispatch_or_record_failure(
            db, request, background_tasks, invoice_id=invoice.id, job_id=job.id,
            actor_id=record.actor_id or "system", force=False,
        )
        db.refresh(invoice)
        db.refresh(job)
        outbox = db.scalar(
            select(ProcessingOutbox)
            .where(ProcessingOutbox.job_id == job.id, ProcessingOutbox.event_type == "process_invoice")
            .limit(1)
        )
    response = _upload_response_for_current_job(response, invoice, job, outbox)
    if dispatch_error:
        response["dispatch_warning"] = "The document is saved; dispatch is pending retry. Check administrator outbox health if it remains queued."
    record.response_body = response
    db.commit()
    return response


def _validate_upload_idempotency_replay(
    db: Session, record: IdempotencyRecord, actor: User, content_sha256: str,
    *, expected_source: str = "upload", expected_vendor_id: str | None = None,
) -> None:
    if record.operation != "invoice_upload":
        raise HTTPException(status_code=409, detail="Idempotency key was already used for a different operation")
    if record.actor_id != actor.id:
        raise HTTPException(status_code=409, detail="Idempotency key is already associated with another user")
    response = record.response_body or {}
    prior_fingerprint = response.get("sha256") if isinstance(response, dict) else None
    if not isinstance(prior_fingerprint, str) and isinstance(response, dict):
        prior_invoice_id = response.get("invoice_id")
        if prior_invoice_id:
            prior_fingerprint = db.scalar(
                select(DocumentFile.sha256)
                .where(DocumentFile.invoice_id == prior_invoice_id)
                .limit(1)
            )
    if not isinstance(prior_fingerprint, str) or not prior_fingerprint:
        raise HTTPException(status_code=409, detail="The prior upload has no stored content fingerprint; it cannot be replayed safely")
    if prior_fingerprint != content_sha256:
        raise HTTPException(status_code=409, detail="Idempotency key was already used with different file content")
    prior_invoice_id = (record.response_body or {}).get("invoice_id")
    prior_invoice = db.get(Invoice, prior_invoice_id) if prior_invoice_id else None
    # Internal uploads may acquire a vendor_id later when asynchronous extraction resolves
    # the supplier. That enrichment does not change the original actor/key/content scope.
    # Vendor uploads are different: their bound vendor is an authorization boundary and
    # must still match exactly on replay.
    scope_mismatch = (
        not prior_invoice
        or prior_invoice.source != expected_source
        or (expected_source == "vendor_upload" and prior_invoice.vendor_id != expected_vendor_id)
    )
    if scope_mismatch:
        raise HTTPException(status_code=409, detail="Idempotency key is bound to a different upload scope")


def _reserve_upload_receipt(
    db: Session,
    *,
    actor_id: str,
    source: str,
    vendor_id: str | None,
    idempotency_key: str | None,
    content_sha256: str,
    filename: str,
    mime_type: str,
    byte_size: int,
    staging_path: Path,
    stored_path: Path,
) -> UploadReceipt:
    # Keep the actor/source/vendor-bound digest stable so status lookup and upload
    # recovery refer to the exact same durable operation without exposing the key hash.
    key_hash = _upload_receipt_key_hash(actor_id, source, vendor_id, idempotency_key)
    receipt = None
    if key_hash:
        receipt = db.scalar(select(UploadReceipt).where(
            UploadReceipt.actor_id == actor_id,
            UploadReceipt.idempotency_key_hash == key_hash,
        ))
        if receipt and receipt.content_sha256 != content_sha256:
            raise HTTPException(status_code=409, detail="This upload key is already associated with different file content")
        if receipt and receipt.state in {"receiving", "staged"}:
            raise HTTPException(status_code=409, detail="An upload with this key is still being recovered; retry later or ask an administrator to run upload recovery")
        if receipt and receipt.state == "committed":
            raise HTTPException(status_code=409, detail="This upload was committed but its replay record is unavailable; ask an administrator to inspect upload recovery")
        if receipt and receipt.state not in {"abandoned", "receiving", "staged", "committed"}:
            raise HTTPException(status_code=409, detail="This upload key has an unsupported recovery state")

    if receipt is None:
        receipt_id = str(uuid.uuid4())
        staging_path = staging_path.parent / f".{receipt_id}.uploading"
        receipt = UploadReceipt(
            id=receipt_id,
            source=source,
            source_reference=key_hash,
            actor_id=actor_id,
            idempotency_key_hash=key_hash,
            content_sha256=content_sha256,
            original_filename=filename,
            mime_type=mime_type,
            byte_size=byte_size,
            staging_path=str(staging_path),
            stored_path=str(stored_path),
            state="receiving",
            invoice_id=None,
        )
        db.add(receipt)
    else:
        staging_path = staging_path.parent / f".{receipt.id}.uploading"
        receipt.source = source
        receipt.source_reference = key_hash
        receipt.content_sha256 = content_sha256
        receipt.original_filename = filename
        receipt.mime_type = mime_type
        receipt.byte_size = byte_size
        receipt.staging_path = str(staging_path)
        receipt.stored_path = str(stored_path)
        receipt.state = "receiving"
        receipt.invoice_id = None
        receipt.last_error_type = None
        receipt.updated_at = datetime.now(timezone.utc)
    append_audit_event(
        db, actor_id=actor_id, action="upload.receipt_reserved",
        object_type="upload_receipt", object_id=receipt.id,
        details={"source": source, "vendor_id": vendor_id, "content_sha256": content_sha256, "byte_size": byte_size, "idempotent": bool(key_hash)},
    )
    try:
        db.commit()
        db.refresh(receipt)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An upload with this key is already being processed; retry safely with the same key") from None
    return receipt


async def _create_upload(
    file: UploadFile, request: Request, db: Session, actor: User,
    background_tasks: BackgroundTasks, idempotency_key: str | None,
    *, source: str = "upload", vendor_id: str | None = None,
) -> dict:
    safe_name = Path((file.filename or "invoice.pdf").replace("\\", "/")).name[:255]
    if not safe_name or safe_name in {"..", "."} or any(ord(character) < 32 for character in safe_name):
        raise HTTPException(status_code=400, detail="A safe filename is required")
    actor_id = actor.id
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Maximum upload size is {settings.max_upload_mb} MB")
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    mime_type, suffix = _verify_document(data, safe_name)
    digest = sha256(data).hexdigest()
    if idempotency_key:
        existing = db.get(IdempotencyRecord, idempotency_key)
        if existing:
            _validate_upload_idempotency_replay(
                db, existing, actor, digest, expected_source=source, expected_vendor_id=vendor_id,
            )
            return _replay_upload_response(db, existing, request, background_tasks)
    try:
        malware_result = scan_submission(data)
    except MalwareScanSizeError:
        db.rollback()
        raise HTTPException(status_code=413, detail="The document exceeds the configured malware scanner stream limit; no invoice was stored or queued.") from None
    except MalwareScannerError:
        db.rollback()
        raise HTTPException(status_code=503, detail="The configured malware scanner is unavailable; no invoice was stored or queued.") from None
    if malware_result.status == "infected":
        source_reference = sha256(f"{source}:{actor_id}:{vendor_id or ''}:{idempotency_key}".encode("utf-8")).hexdigest() if idempotency_key else None
        try:
            quarantined = record_quarantined_upload(
                db,
                quarantine_dir=request.app.state.quarantine_dir,
                data=data,
                filename=safe_name,
                source=source,
                actor_id=actor_id,
                source_reference=source_reference,
                scan=malware_result,
            )
        except ValueError:
            db.rollback()
            raise HTTPException(status_code=409, detail="This upload key was already associated with a different quarantined file.") from None
        raise HTTPException(status_code=422, detail=f"Malware was detected; the document was quarantined for administrator review (reference {quarantined.id}). No invoice was created.")

    duplicate_hash_count = db.query(DocumentFile).filter(DocumentFile.sha256 == digest).count()
    db.rollback()  # Finish the read transaction before durable filesystem work.

    invoice_id = str(uuid.uuid4())
    storage_dir = Path(request.app.state.storage_dir).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_seed = str(uuid.uuid4())
    staging_path = storage_dir / f".{receipt_seed}.uploading"
    stored_path = storage_dir / f"{invoice_id}{suffix}"
    receipt = _reserve_upload_receipt(
        db,
        actor_id=actor_id,
        source=source,
        vendor_id=vendor_id,
        idempotency_key=idempotency_key,
        content_sha256=digest,
        filename=safe_name,
        mime_type=mime_type,
        byte_size=len(data),
        staging_path=staging_path,
        stored_path=stored_path,
    )
    staging_path = Path(receipt.staging_path)
    try:
        staging_path = write_upload_staging_file(storage_dir, receipt.id, data)
        receipt.staging_path = str(staging_path)
        receipt.state = "staged"
        receipt.updated_at = datetime.now(timezone.utc)
        db.commit()
        stored_path = promote_upload_staging_file(staging_path, stored_path, storage_dir)
    except Exception as exc:
        abandon_upload_receipt(db, receipt.id, storage_dir, type(exc).__name__)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=500, detail="The uploaded document could not be safely staged in protected storage") from exc

    invoice = Invoice(
        id=invoice_id, invoice_number=f"PENDING-{invoice_id[:8]}", source=source, vendor_id=vendor_id,
        processing_status="queued", workflow_status="processing", is_synthetic=False,
    )
    db.add(invoice)
    try:
        db.flush()
    except Exception as exc:
        abandon_upload_receipt(db, receipt.id, storage_dir, type(exc).__name__)
        raise
    document = DocumentFile(
        invoice_id=invoice.id, original_filename=safe_name, stored_path=str(stored_path.resolve()),
        mime_type=mime_type, sha256=digest, byte_size=len(data),
    )
    job = ProcessingJob(invoice_id=invoice.id, state="queued", stage="queued")
    db.add_all([document, job])
    try:
        db.flush()
        outbox = enqueue_processing_event(db, job_id=job.id, invoice_id=invoice.id, actor_id=actor_id)
        append_audit_event(
            db, actor_id=actor_id, action="invoice.uploaded", object_type="invoice", object_id=invoice.id,
            details={
                "filename": safe_name, "mime_type": mime_type, "sha256": digest,
                "byte_size": len(data), "source": source, "vendor_id": vendor_id,
                "exact_hash_candidates_before_upload": duplicate_hash_count,
            },
        )
        db.flush()
    except Exception as exc:
        abandon_upload_receipt(db, receipt.id, storage_dir, type(exc).__name__)
        raise

    response = {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "job_id": job.id,
        "job_state": "queued",
        "job_stage": "queued",
        "processing_status": "queued",
        "workflow_status": "processing",
        "filename": safe_name,
        "sha256": digest,
        "exact_hash_candidates_before_upload": duplicate_hash_count,
        "malware_scan_status": malware_result.status,
        "malware_scan_engine": malware_result.engine,
        "message": "Document saved. Processing is queued; status will update separately. No payment will be initiated.",
    }
    if malware_result.status == "not_configured":
        response["security_warning"] = "Malware scanning is disabled in this non-production environment; do not upload untrusted documents."
    receipt.state = "committed"
    receipt.invoice_id = invoice.id
    receipt.last_error_type = None
    receipt.updated_at = datetime.now(timezone.utc)
    if idempotency_key:
        db.add(IdempotencyRecord(
            key=idempotency_key, operation="invoice_upload", actor_id=actor_id,
            response_body=response,
        ))
    try:
        db.commit()
    except IntegrityError as exc:
        abandon_upload_receipt(db, receipt.id, storage_dir, type(exc).__name__)
        if idempotency_key:
            existing = db.get(IdempotencyRecord, idempotency_key)
            if existing:
                _validate_upload_idempotency_replay(db, existing, actor, digest)
                return _replay_upload_response(db, existing, request, background_tasks)
        raise
    except Exception as exc:
        # If the commit result is unknown, this helper rechecks document references before
        # it can remove anything. On database unavailability it leaves the file for recovery.
        abandon_upload_receipt(db, receipt.id, storage_dir, type(exc).__name__)
        raise

    dispatch_error = _dispatch_or_record_failure(
        db, request, background_tasks, invoice_id=invoice.id, job_id=job.id, actor_id=actor_id,
    )
    db.refresh(invoice)
    db.refresh(job)
    outbox = db.scalar(select(ProcessingOutbox).where(ProcessingOutbox.job_id == job.id).limit(1))
    if outbox:
        db.refresh(outbox)
    response = _upload_response_for_current_job(response, invoice, job, outbox)
    if dispatch_error:
        response["dispatch_warning"] = "The document is saved and the processing job is queued; the outbox will retry dispatch."
    if idempotency_key:
        record = db.get(IdempotencyRecord, idempotency_key)
        if record:
            record.response_body = response
            db.commit()
    return response


@router.post(
    "/vendor/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload an invoice for the authenticated vendor account",
    description="Vendor users may submit invoices only. They cannot list invoices or download source documents.",
)
async def upload_vendor_invoice(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=160),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("vendor")),
):
    if {role.name for role in user.roles} != {"vendor"}:
        raise HTTPException(status_code=403, detail="Vendor upload requires a vendor-only account")
    if not user.vendor_id:
        raise HTTPException(status_code=403, detail="This vendor account is not bound to a vendor record")
    vendor = db.get(Vendor, user.vendor_id)
    if not vendor or not vendor.is_active:
        raise HTTPException(status_code=403, detail="This vendor account is not bound to an active vendor")
    return await _create_upload(
        file, request, db, user, background_tasks, idempotency_key,
        source="vendor_upload", vendor_id=vendor.id,
    )


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_invoice(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=160),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "ap_clerk", "reviewer")),
):
    return await _create_upload(file, request, db, user, background_tasks, idempotency_key)


@router.post("/bulk-upload", status_code=status.HTTP_202_ACCEPTED)
async def bulk_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "ap_clerk", "reviewer")),
):
    if len(files) > 20:
        raise HTTPException(status_code=413, detail="A single batch may contain up to 20 files")
    results = []
    for file in files:
        results.append(await _create_upload(file, request, db, user, background_tasks, None))
    return {"accepted": len(results), "results": results}


@router.get(
    "/upload-status",
    response_model=UploadOperationStatusResponse,
    summary="Read the durable state of a prior upload operation",
    description=(
        "Looks up an upload by the Idempotency-Key request header. Results are bound to the "
        "authenticated actor and upload source/vendor scope. This endpoint is read-only: it "
        "does not dispatch, retry, or mutate an upload. Missing and out-of-scope keys are indistinguishable."
    ),
)
def get_upload_operation_status(
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=160),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(*READ_ROLES, "vendor")),
):
    """Return only the calling actor's durable receipt, invoice, job and dispatch state."""
    response.headers["Cache-Control"] = "no-store"
    role_names = {role.name for role in user.roles}
    if "vendor" in role_names:
        if role_names != {"vendor"} or not user.vendor_id:
            raise HTTPException(status_code=403, detail="Vendor upload status requires a vendor-only account")
        vendor = db.get(Vendor, user.vendor_id)
        if not vendor or not vendor.is_active:
            raise HTTPException(status_code=403, detail="This vendor account is not bound to an active vendor")
        source, vendor_id = "vendor_upload", vendor.id
    else:
        source, vendor_id = "upload", None

    key_hash = _upload_receipt_key_hash(user.id, source, vendor_id, idempotency_key)
    receipt = db.scalar(
        select(UploadReceipt)
        .where(
            UploadReceipt.actor_id == user.id,
            UploadReceipt.source == source,
            UploadReceipt.idempotency_key_hash == key_hash,
        )
        .limit(1)
    )
    # IdempotencyRecord keeps the original key so same-key POST replay remains compatible.
    # Filter by actor before reading the response and make cross-user/missing results identical.
    record = db.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.key == idempotency_key,
            IdempotencyRecord.actor_id == user.id,
            IdempotencyRecord.operation == "invoice_upload",
        )
        .limit(1)
    )
    record_body = record.response_body if record and isinstance(record.response_body, dict) else {}
    record_invoice_id = record_body.get("invoice_id") if isinstance(record_body.get("invoice_id"), str) else None
    receipt_invoice_id = receipt.invoice_id if receipt else None
    if record_invoice_id and receipt_invoice_id and record_invoice_id != receipt_invoice_id:
        return {
            "status": "recovery_required", "receipt_state": receipt.state,
            "original_filename": receipt.original_filename, "receipt_updated_at": receipt.updated_at,
        }

    invoice_id = record_invoice_id or receipt_invoice_id
    invoice = db.get(Invoice, invoice_id) if invoice_id else None
    if invoice and (invoice.source != source or (source == "vendor_upload" and invoice.vendor_id != vendor_id)):
        # Do not reveal that a key exists in another source/vendor scope.
        return {"status": "not_found"}

    def result_payload(
        operation_status: str,
        *,
        include_invoice: Invoice | None = None,
        job: ProcessingJob | None = None,
        outbox: ProcessingOutbox | None = None,
        document: DocumentFile | None = None,
    ) -> dict:
        return {
            "status": operation_status,
            "receipt_state": receipt.state if receipt else None,
            "original_filename": receipt.original_filename if receipt else (document.original_filename if document else None),
            "receipt_updated_at": receipt.updated_at if receipt else None,
            "invoice_id": include_invoice.id if include_invoice else None,
            "invoice_number": include_invoice.invoice_number if include_invoice else None,
            "processing_status": include_invoice.processing_status if include_invoice else None,
            "workflow_status": include_invoice.workflow_status if include_invoice else None,
            "job_id": job.id if job else None,
            "job_state": job.state if job else None,
            "job_stage": job.stage if job else None,
            "dispatch_state": outbox.state if outbox else None,
            "dispatch_attempts": outbox.attempts if outbox else None,
        }

    if receipt and receipt.state in {"receiving", "staged"}:
        if invoice or record:
            return result_payload("recovery_required", include_invoice=invoice)
        return result_payload("in_progress")

    if receipt and receipt.state == "abandoned" and not invoice:
        return result_payload("abandoned")

    if not receipt and not (record and invoice):
        # This is intentionally the same response for unknown keys and records outside scope.
        return {"status": "not_found"}

    if not invoice:
        if receipt and receipt.state == "committed":
            return result_payload("recovery_required")
        if receipt and receipt.state == "abandoned":
            return result_payload("abandoned")
        return result_payload("recovery_required") if receipt else {"status": "not_found"}

    recorded_job_id = record_body.get("job_id") if isinstance(record_body.get("job_id"), str) else None
    job = db.get(ProcessingJob, recorded_job_id) if recorded_job_id else None
    if job and job.invoice_id != invoice.id:
        job = None
    if not job:
        job = db.scalar(
            select(ProcessingJob)
            .where(ProcessingJob.invoice_id == invoice.id)
            .order_by(ProcessingJob.queued_at.asc())
            .limit(1)
        )
    document = db.scalar(
        select(DocumentFile).where(DocumentFile.invoice_id == invoice.id).order_by(DocumentFile.id.asc()).limit(1)
    )
    if not job:
        return result_payload("recovery_required", include_invoice=invoice, document=document)

    outbox = db.scalar(
        select(ProcessingOutbox)
        .where(ProcessingOutbox.job_id == job.id, ProcessingOutbox.event_type == "process_invoice")
        .limit(1)
    )
    if receipt and receipt.state != "committed":
        # A durable invoice paired with a non-committed receipt needs operator reconciliation.
        return result_payload("recovery_required", include_invoice=invoice, job=job, outbox=outbox, document=document)
    return result_payload("committed", include_invoice=invoice, job=job, outbox=outbox, document=document)


@router.get("")
def list_invoices(
    search: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(default=None, alias="status", max_length=40),
    match_outcome: str | None = Query(default=None, max_length=40),
    vendor_id: str | None = Query(default=None),
    min_risk: float | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles(*READ_ROLES)),
):
    query = select(Invoice)
    if search:
        term = f"%{search.strip()}%"
        query = query.outerjoin(Vendor, Vendor.id == Invoice.vendor_id).where(or_(
            Invoice.invoice_number.ilike(term), Invoice.source.ilike(term),
            Invoice.workflow_status.ilike(term), Invoice.processing_status.ilike(term),
            Vendor.name.ilike(term),
        ))
    if status_filter:
        query = query.where(or_(Invoice.workflow_status == status_filter, Invoice.processing_status == status_filter))
    if vendor_id:
        query = query.where(Invoice.vendor_id == vendor_id)
    if min_risk is not None:
        query = query.where(Invoice.heuristic_risk_score >= min_risk)
    if match_outcome:
        query = query.where(Invoice.id.in_(
            select(InvoiceMatchResult.invoice_id).where(InvoiceMatchResult.outcome == match_outcome)
        ))
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = db.scalars(query.order_by(Invoice.created_at.desc()).offset(offset).limit(limit)).all()
    return {"items": invoice_summaries(db, rows), "total": int(total), "limit": limit, "offset": offset}


@router.get("/{invoice_id}")
def get_invoice(
    invoice_id: str, db: Session = Depends(get_db),
    _user: User = Depends(require_roles(*READ_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice_summary(db, invoice, include_detail=True)


@router.get("/{invoice_id}/document")
def get_document(
    invoice_id: str, request: Request, db: Session = Depends(get_db),
    _user: User = Depends(require_roles(*READ_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or not invoice.documents:
        raise HTTPException(status_code=404, detail="Invoice document not found")
    document = invoice.documents[0]
    path = Path(document.stored_path).resolve()
    if not path.is_relative_to(request.app.state.storage_dir.resolve()):
        raise HTTPException(status_code=403, detail="Document path is outside configured storage")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Stored invoice file is missing")
    return FileResponse(
        path, media_type=document.mime_type, filename=document.original_filename,
        content_disposition_type="inline",
    )


@router.get("/{invoice_id}/document/pages/{page_number}")
def get_document_page(
    invoice_id: str, page_number: int, request: Request,
    db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice or not invoice.documents:
        raise HTTPException(status_code=404, detail="Invoice document not found")
    document = invoice.documents[0]
    path = Path(document.stored_path).resolve()
    if not path.is_relative_to(request.app.state.storage_dir.resolve()):
        raise HTTPException(status_code=404, detail="Stored document is unavailable")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Stored document is unavailable")
    try:
        content = render_document_page(path.read_bytes(), document.original_filename, document.mime_type, page_number)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=415, detail="Document page preview could not be generated within safe processing limits") from exc
    return Response(content, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/{invoice_id}/extraction")
def get_extraction(
    invoice_id: str, db: Session = Depends(get_db),
    _user: User = Depends(require_roles(*READ_ROLES)),
):
    if not db.get(Invoice, invoice_id):
        raise HTTPException(status_code=404, detail="Invoice not found")
    extraction = latest_extraction(db, invoice_id)
    if not extraction:
        raise HTTPException(status_code=404, detail="No extraction result is available yet")
    return {
        "invoice_id": invoice_id, "fields": extraction.extracted_fields,
        "raw_text": extraction.raw_text, "tokens": extraction.tokens,
        "validation_errors": extraction.validation_errors,
        "engine_name": extraction.engine_name, "engine_version": extraction.engine_version,
        "version": extraction.extraction_version,
    }


@router.patch("/{invoice_id}/extraction")
def correct_extraction(
    invoice_id: str, payload: InvoiceCorrectionRequest, db: Session = Depends(get_db),
    user: User = Depends(require_roles(*WRITE_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    extraction = latest_extraction(db, invoice_id)
    if not invoice or not extraction:
        raise HTTPException(status_code=404, detail="Invoice or extraction not found")
    unknown = set(payload.values) - ALLOWED_FIELDS
    if unknown:
        raise HTTPException(status_code=422, detail={"message": "Unsupported field correction", "fields": sorted(unknown)})

    fields = {key: dict(value) if isinstance(value, dict) else value for key, value in extraction.extracted_fields.items()}
    previous_values: dict[str, object] = {}
    for key, raw_value in payload.values.items():
        previous_values[key] = (fields.get(key) or {}).get("value") if isinstance(fields.get(key), dict) else fields.get(key)
        if key == "line_items":
            if not isinstance(raw_value, list):
                raise HTTPException(status_code=422, detail="line_items correction must be a list")
            prior_lines = fields.get("line_items") if isinstance(fields.get("line_items"), list) else []
            normalized_lines = []
            for index, line in enumerate(raw_value, start=1):
                prior_line = prior_lines[index - 1] if index - 1 < len(prior_lines) and isinstance(prior_lines[index - 1], dict) else {}
                try:
                    quantity = Decimal(str(line["quantity"]))
                    unit_price = Decimal(str(line["unit_price"]))
                    line_total = Decimal(str(line.get("line_total", quantity * unit_price)))
                    description = str(line["description"]).strip()
                    if quantity <= 0 or unit_price < 0 or not description:
                        raise ValueError
                except (KeyError, InvalidOperation, ValueError, TypeError):
                    raise HTTPException(status_code=422, detail=f"Invalid corrected line item at index {index}")
                normalized_lines.append({
                    "line_number": index, "sku": line.get("sku"), "description": description,
                    "quantity": str(quantity), "unit_price": str(unit_price),
                    "tax_rate": str(line.get("tax_rate", "0")), "tax_amount": line.get("tax_amount"),
                    "line_total": str(line_total), "confidence": None,
                    "source_page": prior_line.get("source_page"), "bbox": prior_line.get("bbox"),
                    "validation_status": "human_corrected",
                    "confidence_basis": "Human-entered value; source coordinates are retained for audit context, not represented as model confidence.",
                    "source_line": "human-corrected", "extraction_method": "human_correction",
                })
            fields[key] = normalized_lines
            continue

        existing = fields.get(key) if isinstance(fields.get(key), dict) else {}
        new_value = str(raw_value).strip() if raw_value is not None else None
        existing.update({
            "value": new_value, "normalized_value": new_value,
            "validation_status": "human_corrected", "extraction_method": "human_correction",
            "human_correction": {
                "reviewer_id": user.id, "reason": payload.reason,
                "corrected_at": datetime.now(timezone.utc).isoformat(),
            },
        })
        fields[key] = existing

    recalculated_codes = {
        "line_item_arithmetic", "line_subtotal_difference", "invoice_total_arithmetic",
    }
    new_errors = [
        item for item in extraction.validation_errors
        if item.get("code") not in recalculated_codes
        and not any(item.get("field") == key for key in payload.values)
    ]
    for required in ("vendor_name", "invoice_number", "issue_date", "total_amount"):
        entry = fields.get(required)
        value = entry.get("normalized_value", entry.get("value")) if isinstance(entry, dict) else entry
        if (value is None or not str(value).strip()) and not any(item.get("code") == f"missing_{required}" for item in new_errors):
            new_errors.append({
                "code": f"missing_{required}", "field": required,
                "message": f"Required field {required.replace('_', ' ')} is missing after correction.",
            })
    if "line_items" in payload.values and not payload.values["line_items"]:
        new_errors.append({"code": "no_line_items", "field": "line_items", "message": "No line items were provided in the correction."})
    else:
        line_items = fields.get("line_items") if isinstance(fields.get("line_items"), list) else []
        line_sum = Decimal("0")
        for index, line in enumerate(line_items, start=1):
            try:
                quantity = Decimal(str(line["quantity"]))
                unit_price = Decimal(str(line["unit_price"]))
                line_total = Decimal(str(line["line_total"]))
            except (KeyError, InvalidOperation, ValueError, TypeError):
                continue
            expected = (quantity * unit_price).quantize(Decimal("0.01"))
            line_sum += line_total
            if abs(expected - line_total) > Decimal("0.05"):
                new_errors.append({
                    "code": "line_item_arithmetic", "field": "line_items", "line_number": index,
                    "message": f"Line {index}: quantity × unit price is {expected}, but the extracted line total is {line_total}.",
                })
        subtotal_field = fields.get("subtotal")
        subtotal_value = subtotal_field.get("normalized_value", subtotal_field.get("value")) if isinstance(subtotal_field, dict) else subtotal_field
        try:
            subtotal_number = Decimal(str(subtotal_value)) if subtotal_value is not None else None
        except (InvalidOperation, ValueError, TypeError):
            subtotal_number = None
        if subtotal_number is not None and line_items and abs(line_sum - subtotal_number) > Decimal("0.05"):
            new_errors.append({
                "code": "line_subtotal_difference", "field": "subtotal",
                "message": f"Parsed line total {line_sum} differs from stated subtotal {subtotal_number}.",
            })

        def field_decimal(key: str) -> Decimal | None:
            value = fields.get(key)
            if isinstance(value, dict):
                value = value.get("normalized_value", value.get("value"))
            try:
                return Decimal(str(value)) if value is not None else None
            except (InvalidOperation, ValueError, TypeError):
                return None

        subtotal = field_decimal("subtotal")
        tax_amount = field_decimal("tax_amount")
        total_amount = field_decimal("total_amount")
        shipping = field_decimal("shipping_amount") or Decimal("0")
        discount = field_decimal("discount_amount") or Decimal("0")
        if subtotal is not None and tax_amount is not None and total_amount is not None:
            calculated = subtotal + tax_amount + shipping - discount
            if abs(calculated - total_amount) > Decimal("0.05"):
                new_errors.append({
                    "code": "invoice_total_arithmetic", "field": "total_amount",
                    "message": f"Subtotal + tax + shipping - discount = {calculated}, but invoice total is {total_amount}.",
                })

    version = db.query(ExtractionResult).filter_by(invoice_id=invoice_id).count() + 1
    corrected = ExtractionResult(
        invoice_id=invoice_id, raw_text=extraction.raw_text, extracted_fields=fields,
        tokens=extraction.tokens, validation_errors=new_errors,
        engine_name=extraction.engine_name, engine_version=extraction.engine_version + "; human-review-overlay",
        extraction_version=version,
    )
    db.add(corrected)
    for key, value in payload.values.items():
        if key == "line_items":
            invoice.lines.clear()
            for index, line in enumerate(fields[key], start=1):
                invoice.lines.append(InvoiceLine(
                    line_number=index, sku=line.get("sku"), description=line["description"],
                    quantity=Decimal(line["quantity"]), unit_price=Decimal(line["unit_price"]),
                    tax_rate=Decimal(line.get("tax_rate", "0")), line_total=Decimal(line["line_total"]),
                ))
        elif key in {"vendor_name", "vendor_tax_id"}:
            matched_vendor = _resolve_vendor(db, fields)
            invoice.vendor_id = matched_vendor.id if matched_vendor else None
        elif key == "invoice_number":
            invoice.invoice_number = str(value)
        elif key == "purchase_order_number":
            po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.number == str(value)))
            invoice.purchase_order_id = po.id if po else None
        elif key in {"issue_date", "due_date"}:
            try:
                setattr(invoice, key, date.fromisoformat(str(value)[:10]) if value else None)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{key} must use ISO date format YYYY-MM-DD")
        elif key == "currency":
            currency = str(value).upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise HTTPException(status_code=422, detail="currency must be a three-letter ISO-like code")
            invoice.currency = currency
        elif key in {"subtotal", "tax_amount", "shipping_amount", "discount_amount", "total_amount"}:
            try:
                number = Decimal(str(value)) if value not in (None, "") else None
                if number is not None and number < 0:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise HTTPException(status_code=422, detail=f"{key} must be a non-negative decimal")
            setattr(invoice, key, number)
    from app.models import ModelFeedback
    db.add(ModelFeedback(
        invoice_id=invoice_id, reviewer_id=user.id,
        correction_payload={"previous": previous_values, "corrected": payload.values},
        reason=payload.reason, approved_for_training=False,
    ))
    append_audit_event(
        db, actor_id=user.id, action="invoice.extraction_corrected", object_type="invoice",
        object_id=invoice_id,
        details={"fields": sorted(payload.values), "reason": payload.reason, "extraction_version": version},
    )
    db.commit()
    return {
        "invoice_id": invoice_id, "extraction_version": version,
        "corrected_fields": sorted(payload.values),
        "confidence_note": "Human corrections are tracked separately; model/OCR confidence was not silently increased.",
    }


@router.post("/{invoice_id}/validate")
def validate_invoice(
    invoice_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles(*WRITE_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    result = evaluate_invoice(db, invoice, actor_id=user.id)
    db.commit()
    return {"invoice_id": invoice_id}


@router.post("/{invoice_id}/match")
def run_matching(
    invoice_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles(*WRITE_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    result = evaluate_invoice(db, invoice, actor_id=user.id)
    db.commit()
    return {
        "invoice_id": invoice_id, "match": result["match"],
        "heuristic_risk_score": result["heuristic_risk_score"],
        "risk_band": result["risk_band"], "fraud_signals": result["signals"],
        "workflow_status": invoice.workflow_status,
    }


@router.get("/{invoice_id}/match")
def get_match_results(
    invoice_id: str, db: Session = Depends(get_db), _user: User = Depends(require_roles(*READ_ROLES)),
):
    if not db.get(Invoice, invoice_id):
        raise HTTPException(status_code=404, detail="Invoice not found")
    result = latest_match(db, invoice_id)
    if result:
        return {
            "invoice_id": invoice_id, "outcome": result.outcome,
            "details": result.details, "evaluated_at": result.evaluated_at.isoformat(),
        }
    return {"invoice_id": invoice_id, "outcome": None, "details": None}


@router.post("/{invoice_id}/fraud-analysis")
def run_fraud_analysis(
    invoice_id: str, db: Session = Depends(get_db), user: User = Depends(require_roles(*WRITE_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    result = evaluate_invoice(db, invoice, actor_id=user.id)
    db.commit()
    return {
        "invoice_id": invoice_id, "heuristic_risk_score": result["heuristic_risk_score"],
        "risk_band": result["risk_band"], "is_probability": False,
        "signals": result["signals"], "model_versions": result["model_versions"],
    }


@router.post("/{invoice_id}/reprocess", status_code=status.HTTP_202_ACCEPTED)
def reprocess_invoice(
    invoice_id: str, request: Request, background_tasks: BackgroundTasks,
    db: Session = Depends(get_db), user: User = Depends(require_roles(*WRITE_ROLES)),
):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    active = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.invoice_id == invoice_id, ProcessingJob.state.in_(["queued", "processing"]))
        .limit(1)
    )
    if active:
        raise HTTPException(status_code=409, detail="Invoice already has an active processing job")
    job = ProcessingJob(invoice_id=invoice_id, state="queued", stage="queued")
    invoice.processing_status, invoice.workflow_status = "queued", "processing"
    db.add(job)
    db.flush()
    outbox = enqueue_processing_event(db, job_id=job.id, invoice_id=invoice.id, actor_id=user.id)
    append_audit_event(
        db, actor_id=user.id, action="invoice.reprocess_requested", object_type="invoice",
        object_id=invoice_id, details={"job_id": job.id},
    )
    db.commit()
    dispatch_error = _dispatch_or_record_failure(
        db, request, background_tasks, invoice_id=invoice.id, job_id=job.id, actor_id=user.id,
    )
    db.refresh(job)
    db.refresh(outbox)
    result = {
        "invoice_id": invoice_id, "job_id": job.id, "state": job.state,
        "stage": job.stage, "dispatch_state": outbox.state,
        "dispatch_attempts": outbox.attempts,
        "message": "A new processing attempt was persisted in the dispatch outbox. No payment is triggered.",
    }
    if dispatch_error:
        result["dispatch_warning"] = "The job is safely queued; the outbox will retry broker dispatch."
    return result
