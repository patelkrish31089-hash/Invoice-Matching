from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "ledgerlens",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
)


@celery_app.task(name="app.tasks.process_invoice", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def process_invoice_task(job_id: str) -> None:
    from app.db import create_database_engine, create_session_factory
    from app.processing import run_processing_job

    engine = create_database_engine(settings.database_url)
    try:
        run_processing_job(job_id, create_session_factory(engine), settings.storage_path)
    finally:
        engine.dispose()
