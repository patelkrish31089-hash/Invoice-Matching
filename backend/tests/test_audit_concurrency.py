from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from app.audit import append_audit_event, verify_audit_chain
from app.db import Base, create_database_engine, create_session_factory
from app.models import AuditEvent


def test_concurrent_sqlite_audit_writers_keep_one_valid_hash_chain(tmp_path):
    database_path = tmp_path / "audit-concurrency.sqlite"
    engine = create_database_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    def append_event(index: int) -> None:
        with session_factory() as db:
            append_audit_event(
                db,
                actor_id=None,
                action="test.concurrent_event",
                object_type="test",
                object_id=str(index),
                details={"index": index},
            )
            db.commit()

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(append_event, range(24)))

        with session_factory() as db:
            events = list(db.scalars(select(AuditEvent).order_by(AuditEvent.sequence.asc())))
            assert [event.sequence for event in events] == list(range(1, 25))
            assert verify_audit_chain(db)["valid"] is True
    finally:
        engine.dispose()
