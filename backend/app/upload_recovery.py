from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.models import DocumentFile, UploadReceipt
from app.storage import safely_remove_storage_file


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def abandon_upload_receipt(db: Session, receipt_id: str, storage_root: Path, error_type: str) -> None:
    """Mark a known-uncommitted receipt retryable without deleting referenced bytes."""
    try:
        db.rollback()
        receipt = db.get(UploadReceipt, receipt_id)
        if not receipt:
            return
        document = db.scalar(select(DocumentFile).where(DocumentFile.stored_path == receipt.stored_path).limit(1))
        if document:
            receipt.state = "committed"
            receipt.invoice_id = document.invoice_id
            db.commit()
            return
        safely_remove_storage_file(receipt.staging_path, storage_root)
        safely_remove_storage_file(receipt.stored_path, storage_root)
        receipt.state = "abandoned"
        receipt.invoice_id = None
        receipt.last_error_type = error_type[:120]
        receipt.updated_at = datetime.now(timezone.utc)
        append_audit_event(
            db, actor_id=receipt.actor_id, action="upload.receipt_abandoned",
            object_type="upload_receipt", object_id=receipt.id,
            details={"content_sha256": receipt.content_sha256, "error_type": error_type[:120]},
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def recover_stale_upload_receipts(
    db: Session,
    storage_root: Path,
    *,
    actor_id: str | None = None,
    older_than_seconds: int = 3600,
    limit: int = 100,
) -> dict[str, int | str]:
    """Reconcile interrupted upload intents without touching committed document files.

    Staged/receiving records older than the grace interval are checked against the live
    document table. Referenced files are retained and the receipt is marked committed;
    unreferenced staging/final files are safely removed and the receipt is made retryable.
    """
    root = storage_root.resolve()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(60, int(older_than_seconds)))
    candidates = db.scalars(
        select(UploadReceipt)
        .where(UploadReceipt.state.in_(("receiving", "staged")))
        .order_by(UploadReceipt.updated_at.asc())
        .limit(max(1, min(int(limit), 1000)))
        .with_for_update(skip_locked=True)
    ).all()
    examined = 0
    abandoned = 0
    recovered_committed = 0
    files_removed = 0
    for receipt in candidates:
        if _utc(receipt.updated_at) > cutoff:
            continue
        examined += 1
        document = db.scalar(
            select(DocumentFile).where(DocumentFile.stored_path == receipt.stored_path).limit(1)
        )
        if document:
            receipt.state = "committed"
            receipt.invoice_id = document.invoice_id
            receipt.last_error_type = None
            recovered_committed += 1
            outcome = "committed_reference_found"
        else:
            removed_stage = safely_remove_storage_file(receipt.staging_path, root)
            removed_final = safely_remove_storage_file(receipt.stored_path, root)
            files_removed += int(removed_stage) + int(removed_final)
            receipt.state = "abandoned"
            receipt.invoice_id = None
            receipt.last_error_type = "recovered_interrupted_upload"
            abandoned += 1
            outcome = "unreferenced_files_removed"
        append_audit_event(
            db,
            actor_id=actor_id,
            action="upload.receipt_recovered",
            object_type="upload_receipt",
            object_id=receipt.id,
            details={
                "outcome": outcome,
                "content_sha256": receipt.content_sha256,
                "byte_size": receipt.byte_size,
                "files_removed": (removed_stage + removed_final) if outcome == "unreferenced_files_removed" else 0,
            },
        )
    if examined:
        db.commit()
    return {
        "examined": examined,
        "abandoned": abandoned,
        "recovered_committed": recovered_committed,
        "files_removed": files_removed,
        "grace_seconds": max(60, int(older_than_seconds)),
        "limit": max(1, min(int(limit), 1000)),
    }
