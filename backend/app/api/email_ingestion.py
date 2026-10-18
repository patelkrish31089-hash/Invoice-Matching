from __future__ import annotations

from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from hashlib import sha256
import hmac
from pathlib import Path
import re
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.config import settings
from app.db import get_db
from app.models import DocumentFile, EmailIngestionEvent, Invoice, ProcessingJob, UploadReceipt
from app.outbox import dispatch_outbox_event, enqueue_processing_event
from app.storage import promote_upload_staging_file, write_upload_staging_file
from app.upload_recovery import abandon_upload_receipt, recover_stale_upload_receipts
from app.services.malware_quarantine import record_quarantined_upload, scan_submission
from app.services.malware_scan import MalwareScannerError, MalwareScanSizeError

router = APIRouter(prefix="/ingestion", tags=["external ingestion"])
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,159}$")


def _configured_senders() -> set[str]:
    return {
        item.strip().casefold()
        for item in (settings.email_ingest_allowed_senders or "").split(",")
        if item.strip()
    }


def _check_configuration() -> tuple[str, set[str]]:
    secret = (settings.email_ingest_webhook_secret or "").strip()
    senders = _configured_senders()
    if len(secret) < 32 or not senders:
        raise HTTPException(
            status_code=503,
            detail="Email ingestion is not configured; a 32-character webhook secret and exact sender allowlist are required. No email body was read.",
        )
    return secret, senders


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Email message exceeds the configured size limit")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="Email message exceeds the configured size limit")
        body.extend(chunk)
    if not body:
        raise HTTPException(status_code=400, detail="Email message body is required")
    return bytes(body)


def _verify_signature(raw_message: bytes, secret: str, timestamp: str | None, signature: str | None, event_id: str) -> None:
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Signed email webhook headers are required")
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid email webhook timestamp") from None
    tolerance = max(30, min(900, int(settings.email_ingest_signature_tolerance_seconds)))
    if abs(int(time.time()) - timestamp_value) > tolerance:
        raise HTTPException(status_code=401, detail="Email webhook signature has expired")
    supplied = signature.strip().lower()
    if supplied.startswith("sha256="):
        supplied = supplied[7:]
    signed_bytes = timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + raw_message
    expected = hmac.new(secret.encode("utf-8"), signed_bytes, sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Email webhook signature is invalid")


def _validate_email_document(data: bytes, filename: str) -> tuple[str, str]:
    from app.api.invoices import _verify_document

    try:
        mime_type, suffix = _verify_document(data, filename)
    except HTTPException:
        raise
    if suffix == ".pdf":
        try:
            import fitz
            with fitz.open(stream=data, filetype="pdf") as document:
                if document.needs_pass:
                    raise HTTPException(status_code=415, detail="Encrypted email PDF attachments are not accepted")
                if len(document) < 1:
                    raise HTTPException(status_code=415, detail="Email PDF attachment has no pages")
                if len(document) > 200:
                    raise HTTPException(status_code=413, detail="Email PDF attachment exceeds the 200-page processing limit")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=415, detail="Email PDF attachment is malformed or unreadable") from None
    elif suffix == ".docx":
        # `_verify_document` already performed bounded OOXML container/XML validation.
        pass
    else:
        try:
            from app.services.document_processing import _check_image_dimensions
            _check_image_dimensions(data)
        except ValueError as exc:
            detail = str(exc)
            code = 413 if "exceed" in detail.lower() else 415
            raise HTTPException(status_code=code, detail=f"Email image attachment rejected: {detail}") from None
    return mime_type, suffix


def _extract_attachments(raw_message: bytes, allowed_senders: set[str]) -> tuple[str, list[tuple[str, bytes]]]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
    except Exception:
        raise HTTPException(status_code=400, detail="Email message could not be parsed") from None
    if message.defects:
        raise HTTPException(status_code=400, detail="Email message has malformed MIME structure")
    sender = parseaddr(str(message.get("From", "")))[1].strip().casefold()
    if not sender or sender not in allowed_senders:
        raise HTTPException(status_code=403, detail="Email sender is not in the configured allowlist")

    attachments: list[tuple[str, bytes]] = []
    total_bytes = 0
    max_attachment_bytes = max(1, min(100, int(settings.max_upload_mb))) * 1024 * 1024
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename or disposition != "attachment":
            continue
        if len(attachments) >= 5:
            raise HTTPException(status_code=413, detail="An email may contain at most five invoice attachments")
        safe_name = Path(str(filename).replace("\\", "/")).name[:255]
        if not safe_name or safe_name in {".", ".."} or any(ord(ch) < 32 for ch in safe_name):
            raise HTTPException(status_code=400, detail="Email contains an unsafe attachment filename")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            raise HTTPException(status_code=400, detail="Email contains an empty or undecodable attachment")
        if len(payload) > max_attachment_bytes:
            raise HTTPException(status_code=413, detail="An email attachment exceeds the configured upload size limit")
        total_bytes += len(payload)
        if total_bytes > max_attachment_bytes:
            raise HTTPException(status_code=413, detail="Combined email attachment size exceeds the configured upload size limit")
        try:
            # Reuse portal file-signature checks and additionally parse/bound the container.
            _validate_email_document(payload, safe_name)
        except HTTPException as exc:
            raise HTTPException(status_code=exc.status_code, detail=f"Email attachment rejected: {exc.detail}") from None
        attachments.append((safe_name, payload))

    if not attachments:
        raise HTTPException(status_code=422, detail="Email contains no supported invoice attachments")
    return sender, attachments


def _existing_event_response(db: Session, event_id: str, payload_hash: str) -> dict:
    existing = db.scalar(select(EmailIngestionEvent).where(EmailIngestionEvent.provider_event_id == event_id))
    if not existing:
        raise HTTPException(status_code=409, detail="Email event could not be safely reserved; retry with the same event ID")
    if existing.payload_sha256 != payload_hash:
        raise HTTPException(status_code=409, detail="Email event ID was already used with different content")
    if existing.state != "completed":
        raise HTTPException(status_code=409, detail="Email event is already being processed; retry after checking ingestion status")
    response = dict(existing.response_body or {})
    response["duplicate_event"] = True
    return response


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _reserve_email_receipt(
    db: Session,
    *,
    event: EmailIngestionEvent,
    attachment_index: int,
    filename: str,
    mime_type: str,
    byte_size: int,
    content_sha256: str,
    staging_path: Path,
    stored_path: Path,
) -> UploadReceipt:
    source_reference = sha256(f"email:{event.provider_event_id}:{attachment_index}".encode("utf-8")).hexdigest()
    receipt = db.scalar(select(UploadReceipt).where(
        UploadReceipt.source == "email",
        UploadReceipt.source_reference == source_reference,
    ))
    if receipt and receipt.content_sha256 != content_sha256:
        raise HTTPException(status_code=409, detail="Email attachment receipt was reused with different content")
    if receipt and receipt.state in {"receiving", "staged"}:
        raise HTTPException(status_code=409, detail="An email attachment is still being recovered; retry after upload recovery")
    if receipt and receipt.state == "committed":
        raise HTTPException(status_code=409, detail="Email attachment was committed but its event response is unavailable; ask an administrator to inspect upload recovery")
    if receipt and receipt.state not in {"abandoned", "receiving", "staged", "committed"}:
        raise HTTPException(status_code=409, detail="Email attachment has an unsupported recovery state")

    if receipt is None:
        receipt_id = str(uuid.uuid4())
        receipt = UploadReceipt(
            id=receipt_id,
            source="email",
            source_reference=source_reference,
            actor_id=None,
            idempotency_key_hash=None,
            content_sha256=content_sha256,
            original_filename=filename,
            mime_type=mime_type,
            byte_size=byte_size,
            staging_path=str(staging_path.parent / f".{receipt_id}.uploading"),
            stored_path=str(stored_path),
            state="receiving",
            invoice_id=None,
        )
        db.add(receipt)
    else:
        receipt.staging_path = str(staging_path.parent / f".{receipt.id}.uploading")
        receipt.stored_path = str(stored_path)
        receipt.original_filename = filename
        receipt.mime_type = mime_type
        receipt.byte_size = byte_size
        receipt.state = "receiving"
        receipt.invoice_id = None
        receipt.last_error_type = None
        receipt.updated_at = datetime.now(timezone.utc)
    append_audit_event(
        db, actor_id=None, action="upload.receipt_reserved",
        object_type="upload_receipt", object_id=receipt.id,
        details={"source": "email", "content_sha256": content_sha256, "byte_size": byte_size},
    )
    try:
        db.commit()
        db.refresh(receipt)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An email attachment with this event ID is already being processed") from None
    return receipt


def _mark_email_event_failed(db: Session, event_id: str, payload_hash: str, error_type: str) -> None:
    try:
        db.rollback()
        event = db.scalar(select(EmailIngestionEvent).where(EmailIngestionEvent.provider_event_id == event_id))
        if not event or event.state == "completed":
            return
        event.state = "failed"
        event.response_body = {"event_state": "failed", "payload_sha256": payload_hash, "error_type": error_type[:120]}
        event.created_at = datetime.now(timezone.utc)
        append_audit_event(
            db, actor_id=None, action="invoice.email_ingestion_failed",
            object_type="email_ingestion_event", object_id=event.id,
            details={"payload_sha256": payload_hash, "error_type": error_type[:120]},
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


@router.post("/email", status_code=status.HTTP_202_ACCEPTED)
async def ingest_email_attachments(
    request: Request,
    background_tasks: BackgroundTasks,
    event_id: str | None = Header(default=None, alias="X-InvoiceGuard-Event-ID"),
    signature: str | None = Header(default=None, alias="X-InvoiceGuard-Signature"),
    timestamp: str | None = Header(default=None, alias="X-InvoiceGuard-Timestamp"),
    db: Session = Depends(get_db),
):
    """Accept a signed RFC822 message from a trusted ingress; never store the email body."""
    secret, allowed_senders = _check_configuration()
    if not event_id or not _EVENT_ID.fullmatch(event_id):
        raise HTTPException(status_code=400, detail="A valid provider event ID is required")
    max_message_bytes = max(1, min(50, int(settings.email_ingest_max_message_mb))) * 1024 * 1024
    raw_message = await _read_limited_body(request, max_message_bytes)
    _verify_signature(raw_message, secret, timestamp, signature, event_id)
    _sender, attachments = _extract_attachments(raw_message, allowed_senders)
    payload_hash = sha256(raw_message).hexdigest()
    now = datetime.now(timezone.utc)

    existing = db.scalar(
        select(EmailIngestionEvent)
        .where(EmailIngestionEvent.provider_event_id == event_id)
        .with_for_update()
    )
    if existing:
        if existing.payload_sha256 != payload_hash:
            raise HTTPException(status_code=409, detail="Email event ID was already used with different content")
        if existing.state == "completed":
            return _existing_event_response(db, event_id, payload_hash)
        if existing.state == "received":
            age_seconds = max(0.0, (now - _utc(existing.created_at)).total_seconds())
            if age_seconds < int(settings.upload_recovery_grace_seconds):
                raise HTTPException(status_code=409, detail="Email event is already being processed; retry after checking ingestion status")
            recover_stale_upload_receipts(
                db,
                Path(request.app.state.storage_dir),
                older_than_seconds=settings.upload_recovery_grace_seconds,
                limit=settings.upload_recovery_batch_size,
            )
        elif existing.state != "failed":
            raise HTTPException(status_code=409, detail="Email event is in an unsupported recovery state")

    scan_results = []
    for _index, (_filename, payload) in enumerate(attachments):
        try:
            scan_results.append(scan_submission(payload))
        except MalwareScanSizeError:
            db.rollback()
            raise HTTPException(status_code=413, detail="An email attachment exceeds the malware scanner stream limit; nothing was stored or queued.") from None
        except MalwareScannerError:
            db.rollback()
            raise HTTPException(status_code=503, detail="The configured malware scanner is unavailable; no email attachment was stored or queued.") from None

    attachment_metadata = [
        {"index": index, "sha256": sha256(payload).hexdigest(), "byte_size": len(payload)}
        for index, (_filename, payload) in enumerate(attachments)
    ]
    reservation = {
        "event_state": "received",
        "payload_sha256": payload_hash,
        "attachments": attachment_metadata,
    }
    if existing:
        event = existing
        event.state = "received"
        event.response_body = reservation
        event.completed_at = None
        event.created_at = now
        try:
            db.commit()
            db.refresh(event)
        except Exception:
            db.rollback()
            raise HTTPException(status_code=503, detail="Email event reservation could not be safely refreshed") from None
    else:
        event = EmailIngestionEvent(
            provider_event_id=event_id,
            payload_sha256=payload_hash,
            state="received",
            response_body=reservation,
            created_at=now,
        )
        db.add(event)
        try:
            # Commit the unique event reservation before any filesystem write. The signed
            # provider event can then be resumed safely after a process or host crash.
            db.commit()
            db.refresh(event)
        except IntegrityError:
            db.rollback()
            winner = db.scalar(select(EmailIngestionEvent).where(EmailIngestionEvent.provider_event_id == event_id))
            if winner and winner.payload_sha256 == payload_hash and winner.state == "completed":
                return _existing_event_response(db, event_id, payload_hash)
            raise HTTPException(status_code=409, detail="Email event is already being processed; retry with the same event ID") from None

    storage_root = Path(request.app.state.storage_dir).resolve()
    storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    reserved_receipts: list[UploadReceipt] = []
    invoice_ids: list[str] = []
    outbox_ids: list[str] = []
    items: list[dict] = []
    quarantined_items: list[dict] = []
    security_warnings: list[str] = []
    upload_plans: list[dict] = []
    response: dict = {}
    try:
        for attachment_index, (safe_name, payload) in enumerate(attachments):
            scan_result = scan_results[attachment_index]
            digest = sha256(payload).hexdigest()
            if scan_result.status == "infected":
                source_reference = sha256(f"email:{event_id}:{attachment_index}".encode("utf-8")).hexdigest()
                quarantine_row = record_quarantined_upload(
                    db,
                    quarantine_dir=request.app.state.quarantine_dir,
                    data=payload,
                    filename=safe_name,
                    source="email",
                    actor_id=None,
                    source_reference=source_reference,
                    scan=scan_result,
                )
                quarantined_items.append({
                    "quarantine_id": quarantine_row.id,
                    "filename": safe_name,
                    "sha256": quarantine_row.sha256,
                    "detection": quarantine_row.detection,
                    "status": "quarantined",
                })
                continue
            if scan_result.status == "not_configured":
                security_warnings.append("Malware scanning is disabled in this non-production environment; untrusted attachments must not be processed.")

            mime_type, suffix = _validate_email_document(payload, safe_name)
            invoice_id = str(uuid.uuid4())
            stored_path = storage_root / f"{invoice_id}{suffix}"
            receipt = _reserve_email_receipt(
                db,
                event=event,
                attachment_index=attachment_index,
                filename=safe_name,
                mime_type=mime_type,
                byte_size=len(payload),
                content_sha256=digest,
                staging_path=storage_root / "pending.uploading",
                stored_path=stored_path,
            )
            reserved_receipts.append(receipt)
            staging_path = Path(receipt.staging_path)
            staging_path = write_upload_staging_file(storage_root, receipt.id, payload)
            receipt.staging_path = str(staging_path)
            receipt.state = "staged"
            receipt.updated_at = datetime.now(timezone.utc)
            db.commit()
            stored_path = promote_upload_staging_file(staging_path, stored_path, storage_root)
            upload_plans.append({
                "receipt": receipt,
                "invoice_id": invoice_id,
                "filename": safe_name,
                "mime_type": mime_type,
                "suffix": suffix,
                "sha256": digest,
                "byte_size": len(payload),
                "stored_path": stored_path,
                "scan": scan_result,
            })

        for plan in upload_plans:
            invoice_id = plan["invoice_id"]
            invoice = Invoice(
                id=invoice_id,
                invoice_number=f"EMAIL-PENDING-{invoice_id[:8]}",
                source="email",
                processing_status="queued",
                workflow_status="processing",
                is_synthetic=False,
            )
            db.add(invoice)
            db.flush()
            db.add(DocumentFile(
                invoice_id=invoice.id,
                original_filename=plan["filename"],
                stored_path=str(plan["stored_path"].resolve()),
                mime_type=plan["mime_type"],
                sha256=plan["sha256"],
                byte_size=plan["byte_size"],
            ))
            job = ProcessingJob(invoice_id=invoice.id, state="queued", stage="queued")
            db.add(job)
            db.flush()
            outbox_event = enqueue_processing_event(db, job_id=job.id, invoice_id=invoice.id, actor_id=None)
            db.flush()
            receipt = plan["receipt"]
            receipt.state = "committed"
            receipt.invoice_id = invoice.id
            receipt.last_error_type = None
            receipt.updated_at = datetime.now(timezone.utc)
            invoice_ids.append(invoice.id)
            outbox_ids.append(outbox_event.id)
            items.append({
                "invoice_id": invoice.id,
                "job_id": job.id,
                "filename": plan["filename"],
                "sha256": plan["sha256"],
                "job_state": "queued",
                "dispatch_state": outbox_event.state,
                "malware_scan_status": plan["scan"].status,
                "malware_scan_engine": plan["scan"].engine,
            })

        response = {
            "source": "email",
            "provider_event_id": event_id,
            "event_state": "completed",
            "duplicate_event": False,
            "attachments_received": len(attachments),
            "queued_attachments": len(items),
            "quarantined_attachments": len(quarantined_items),
            "items": items,
            "quarantined_items": quarantined_items,
            "security_warnings": sorted(set(security_warnings)),
            "payment_execution": False,
            "message": "Email ingestion completed with every attachment either queued after scanning or isolated for malware review. No approval or payment was initiated.",
        }
        event.state = "completed"
        event.response_body = response
        event.completed_at = datetime.now(timezone.utc)
        append_audit_event(
            db,
            actor_id=None,
            action="invoice.email_ingestion_completed",
            object_type="email_ingestion_event",
            object_id=event.id,
            details={
                "attachment_count": len(attachments),
                "queued_attachment_count": len(items),
                "quarantined_attachment_count": len(quarantined_items),
                "invoice_ids": invoice_ids,
                "payload_sha256": payload_hash,
            },
        )
        db.commit()
    except HTTPException as exc:
        for receipt in reserved_receipts:
            abandon_upload_receipt(db, receipt.id, storage_root, type(exc).__name__)
        _mark_email_event_failed(db, event_id, payload_hash, type(exc).__name__)
        raise
    except Exception as exc:
        db.rollback()
        try:
            persisted = db.scalar(select(EmailIngestionEvent).where(EmailIngestionEvent.provider_event_id == event_id))
        except Exception:
            persisted = None
        if persisted and persisted.state == "completed" and persisted.payload_sha256 == payload_hash:
            response = dict(persisted.response_body or {})
        else:
            for receipt in reserved_receipts:
                abandon_upload_receipt(db, receipt.id, storage_root, type(exc).__name__)
            _mark_email_event_failed(db, event_id, payload_hash, type(exc).__name__)
            raise HTTPException(status_code=500, detail="Email ingestion failed; no success was recorded. Retry using the same signed event ID after checking ingestion status.") from exc

    dispatch_failures = []
    if settings.task_mode.lower() == "celery":
        for outbox_id in outbox_ids:
            error_type = dispatch_outbox_event(
                request.app.state.session_factory,
                request.app.state.storage_dir,
                outbox_id,
                force=True,
            )
            if error_type:
                dispatch_failures.append(error_type)
    else:
        for outbox_id in outbox_ids:
            background_tasks.add_task(
                dispatch_outbox_event,
                request.app.state.session_factory,
                request.app.state.storage_dir,
                outbox_id,
                force=True,
            )
    if dispatch_failures:
        response["dispatch_warning"] = "Attachments are saved and queued; processing dispatch will retry. An administrator should inspect the outbox if the jobs remain queued."
        event_row = db.scalar(select(EmailIngestionEvent).where(EmailIngestionEvent.provider_event_id == event_id))
        if event_row:
            event_row.response_body = response
            db.commit()
    return response
