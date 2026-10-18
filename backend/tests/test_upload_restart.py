from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import create_app
from app.models import (
    DocumentFile,
    IdempotencyRecord,
    Invoice,
    ProcessingJob,
    ProcessingOutbox,
    Role,
    UploadReceipt,
    User,
)
from app.security import hash_password


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _create_admin(app) -> None:
    with app.state.session_factory() as db:
        role = Role(name="admin", description="Restart-replay test administrator")
        user = User(
            email="restart-replay@test.local",
            display_name="Restart Replay Admin",
            password_hash=hash_password("RestartReplayTestPass123!"),
            roles=[role],
        )
        db.add_all([role, user])
        db.commit()


def _login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "restart-replay@test.local", "password": "RestartReplayTestPass123!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_committed_upload_replays_same_invoice_after_api_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    dispatched_job_ids: list[str] = []
    monkeypatch.setattr(
        "app.tasks.process_invoice_task.delay",
        lambda job_id: dispatched_job_ids.append(job_id),
    )

    database_url = f"sqlite:///{tmp_path / 'restart-replay.sqlite'}"
    storage_dir = tmp_path / "persistent-storage"
    document_bytes = (PROJECT_ROOT / "data" / "demo_docs" / "invoice_matched.pdf").read_bytes()
    idempotency_key = "restart-replay-same-actor-key-001"
    persisted: dict[str, str] = {}

    first_app = create_app(
        database_url=database_url,
        storage_dir=storage_dir,
        initialize_schema=True,
        seed_demo=False,
    )
    with TestClient(first_app) as first_client:
        _create_admin(first_app)
        headers = {**_login(first_client), "Idempotency-Key": idempotency_key}
        first_response = first_client.post(
            "/api/v1/invoices/upload",
            headers=headers,
            files={"file": ("restart-replay.pdf", document_bytes, "application/pdf")},
        )
        assert first_response.status_code == 202, first_response.text
        # Deliberately discard the response body: the durable DB/storage commit is
        # the only recovery evidence the simulated client keeps across restart.

        with first_app.state.session_factory() as db:
            receipt = db.scalar(select(UploadReceipt))
            invoice = db.scalar(select(Invoice))
            document = db.scalar(select(DocumentFile))
            job = db.scalar(select(ProcessingJob))
            event = db.scalar(select(ProcessingOutbox))
            assert receipt is not None and receipt.state == "committed"
            assert invoice is not None and document is not None and job is not None and event is not None
            assert receipt.invoice_id == invoice.id
            assert document.invoice_id == invoice.id
            assert job.invoice_id == invoice.id
            assert event.job_id == job.id and event.state == "published"
            stored_path = Path(document.stored_path)
            assert stored_path.is_file() and stored_path.read_bytes() == document_bytes
            persisted.update(invoice_id=invoice.id, document_id=document.id, job_id=job.id, path=str(stored_path))

    # Constructing a new app/client on the same disposable DB and volume exercises
    # the same startup recovery path used after an API process restart.
    restarted_app = create_app(
        database_url=database_url,
        storage_dir=storage_dir,
        initialize_schema=True,
        seed_demo=False,
    )
    with TestClient(restarted_app) as restarted_client:
        headers = {**_login(restarted_client), "Idempotency-Key": idempotency_key}
        replay = restarted_client.post(
            "/api/v1/invoices/upload",
            headers=headers,
            files={"file": ("restart-replay.pdf", document_bytes, "application/pdf")},
        )
        assert replay.status_code == 202, replay.text
        replay_payload = replay.json()
        assert replay_payload["invoice_id"] == persisted["invoice_id"]
        assert replay_payload["job_id"] == persisted["job_id"]

        with restarted_app.state.session_factory() as db:
            assert db.query(Invoice).count() == 1
            assert db.query(DocumentFile).count() == 1
            assert db.query(ProcessingJob).count() == 1
            assert db.query(IdempotencyRecord).count() == 1
            assert db.query(UploadReceipt).count() == 1
            assert db.query(ProcessingOutbox).count() == 1
            receipt = db.scalar(select(UploadReceipt))
            assert receipt.state == "committed"
            assert receipt.invoice_id == persisted["invoice_id"]
            document = db.get(DocumentFile, persisted["document_id"])
            assert document is not None
            assert Path(document.stored_path).read_bytes() == document_bytes

    # Replay must not publish a second task or create another document path.
    assert dispatched_job_ids == [persisted["job_id"]]
    assert sorted(path.name for path in storage_dir.iterdir()) == [Path(persisted["path"]).name]
