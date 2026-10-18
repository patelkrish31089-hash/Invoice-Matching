from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AuditEvent


# Stable advisory-lock ID reserved for this application's ordered audit hash chain.
_AUDIT_CHAIN_ADVISORY_LOCK_ID = 0x4C45444745524C45


def append_audit_event(
    db: Session,
    *,
    actor_id: str | None,
    action: str,
    object_type: str,
    object_id: str,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append a hash-linked event. The chain is tamper-evident, not a WORM ledger.

    PostgreSQL's transaction-scoped advisory lock serializes the head lookup through
    the caller's commit. SQLite transactions are started with BEGIN IMMEDIATE by the
    local/test engine, which serializes its single writer before any request reads.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _AUDIT_CHAIN_ADVISORY_LOCK_ID},
        )
    previous = db.scalar(select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1))
    sequence = (previous.sequence + 1) if previous else 1
    previous_hash = previous.event_hash if previous else "0" * 64
    occurred_at = datetime.now(timezone.utc)
    safe_details = details or {}
    canonical = json.dumps(
        {
            "sequence": sequence,
            "actor_id": actor_id,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "details": safe_details,
            "occurred_at": occurred_at.isoformat(),
            "previous_hash": previous_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    event = AuditEvent(
        sequence=sequence,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        details=safe_details,
        occurred_at=occurred_at,
        previous_hash=previous_hash,
        event_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )
    db.add(event)
    return event


def verify_audit_chain(db: Session) -> dict[str, Any]:
    events = list(db.scalars(select(AuditEvent).order_by(AuditEvent.sequence.asc())))
    previous_hash = "0" * 64
    for expected_sequence, event in enumerate(events, start=1):
        canonical = json.dumps(
            {
                "sequence": event.sequence,
                "actor_id": event.actor_id,
                "action": event.action,
                "object_type": event.object_type,
                "object_id": event.object_id,
                "details": event.details,
                "occurred_at": (event.occurred_at if event.occurred_at.tzinfo else event.occurred_at.replace(tzinfo=timezone.utc)).isoformat(),
                "previous_hash": event.previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if event.sequence != expected_sequence or event.previous_hash != previous_hash or event.event_hash != calculated:
            return {"valid": False, "checked_events": expected_sequence, "failed_sequence": event.sequence}
        previous_hash = event.event_hash
    return {"valid": True, "checked_events": len(events), "head_hash": previous_hash}
