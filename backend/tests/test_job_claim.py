from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.db import Base, create_database_engine, create_session_factory
from app.models import Invoice, ProcessingJob
from app.processing import _claim_queued_job


def test_only_one_concurrent_sqlite_worker_claims_a_queued_job(tmp_path):
    database_path = tmp_path / "concurrent-claim.sqlite"
    engine = create_database_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as db:
        invoice = Invoice(invoice_number="INV-CLAIM", source="test")
        db.add(invoice)
        db.flush()
        job = ProcessingJob(invoice_id=invoice.id, state="queued", stage="queued")
        db.add(job)
        db.commit()
        job_id = job.id

    barrier = Barrier(2)

    def claim_job():
        with session_factory() as db:
            barrier.wait(timeout=10)
            claimed = _claim_queued_job(db, job_id)
            if claimed:
                db.commit()
            else:
                db.rollback()
            return claimed

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: claim_job(), range(2)))

    assert sorted(results) == [False, True]
    with session_factory() as db:
        job = db.get(ProcessingJob, job_id)
        assert job.state == "processing"
        assert job.stage == "document_extraction"
        assert job.attempts == 1
        assert job.started_at is not None
        assert job.heartbeat_at is not None
    engine.dispose()
