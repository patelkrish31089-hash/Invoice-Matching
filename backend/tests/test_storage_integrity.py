from __future__ import annotations

from app.models import DocumentFile, Invoice


def test_storage_integrity_is_admin_only_and_reports_missing_orphan_and_unsafe_paths(api, tmp_path):
    client = api["client"]
    storage_root = api["app"].state.storage_dir
    valid_path = storage_root / "nested" / "referenced.pdf"
    valid_path.parent.mkdir(parents=True, exist_ok=True)
    valid_path.write_bytes(b"synthetic referenced document")
    (storage_root / "orphan.bin").write_bytes(b"synthetic orphan")
    outside_path = tmp_path / "outside.pdf"
    outside_path.write_bytes(b"synthetic unsafe reference")

    with api["app"].state.session_factory() as db:
        invoice = Invoice(invoice_number="STORAGE-INTEGRITY-TEST", currency="USD", workflow_status="review_required")
        db.add(invoice)
        db.flush()
        invoice_id = invoice.id
        db.add_all([
            DocumentFile(
                invoice_id=invoice_id, original_filename="referenced.pdf", stored_path=str(valid_path),
                mime_type="application/pdf", sha256="1" * 64, byte_size=valid_path.stat().st_size,
            ),
            DocumentFile(
                invoice_id=invoice_id, original_filename="missing.pdf", stored_path=str(storage_root / "missing.pdf"),
                mime_type="application/pdf", sha256="2" * 64, byte_size=10,
            ),
            DocumentFile(
                invoice_id=invoice_id, original_filename="outside.pdf", stored_path=str(outside_path),
                mime_type="application/pdf", sha256="3" * 64, byte_size=outside_path.stat().st_size,
            ),
        ])
        db.commit()

    forbidden = client.get("/api/v1/admin/storage-integrity", headers=api["auditor"])
    assert forbidden.status_code == 403

    response = client.get("/api/v1/admin/storage-integrity", headers=api["admin"])
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "attention_required"
    assert report["missing_referenced_files"] >= 1
    assert report["orphan_files"] >= 1
    assert report["unsafe_database_paths"] >= 1
    assert any(sample["filename"] == "missing.pdf" for sample in report["missing_samples"])
    assert any(sample["relative_path"].endswith("orphan.bin") for sample in report["orphan_samples"])
    assert all("outside.pdf" not in sample.get("relative_path", "") for sample in report["orphan_samples"])
    assert report["note"].startswith("Read-only snapshot comparison")
    assert not report["scan_truncated"]
