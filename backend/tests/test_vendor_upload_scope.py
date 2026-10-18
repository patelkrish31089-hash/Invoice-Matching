from __future__ import annotations

import pymupdf as fitz
from sqlalchemy import select

from app.config import settings
from app.models import Invoice, ReviewCase, Role, User, Vendor
from app.processing import run_processing_job


def _synthetic_invoice_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    lines = [
        "Vendor: Acme Systems LLC",
        "Tax ID: 12-3456789",
        "Invoice Number: INV-VENDOR-100",
        "Invoice Date: 2026-10-08",
        "PO Number: PO-VENDOR-100",
        "Description | Qty | Unit Price | Amount",
        "SKU-1 | Widget Gear | 1 | 25.00 | 25.00",
        "Subtotal: 25.00",
        "Tax: 0.00",
        "Grand Total: 25.00 USD",
    ]
    for index, line in enumerate(lines):
        page.insert_text((55, 70 + index * 27), line, fontsize=11)
    payload = document.tobytes()
    document.close()
    return payload


def _add_vendors(app) -> tuple[str, str]:
    with app.state.session_factory() as db:
        acme = Vendor(
            code="ACME-SYN-1", name="Acme Systems LLC", aliases=["Acme Systems"],
            tax_identifier="12-3456789", is_active=True,
        )
        harbor = Vendor(
            code="HARBOR-SYN-2", name="Harbor Industrial Supply", aliases=["Harbor Industrial"],
            tax_identifier="98-7654321", is_active=True,
        )
        db.add_all([acme, harbor])
        db.commit()
        return acme.id, harbor.id


def _provision_vendor(api, vendor_id: str, *, email: str = "vendor@test.local") -> tuple[dict, dict[str, str]]:
    response = api["client"].post(
        "/api/v1/admin/users",
        headers=api["admin"],
        json={
            "email": email,
            "display_name": "Bound Vendor Account",
            "password": "VendorPass123!",
            "roles": ["vendor"],
            "vendor_id": vendor_id,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["vendor_id"] == vendor_id
    login = api["client"].post(
        "/api/v1/auth/login",
        json={"email": email, "password": "VendorPass123!"},
    )
    assert login.status_code == 200, login.text
    return payload, {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_vendor_upload_stays_bound_and_supplier_mismatch_requires_human_review(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    monkeypatch.setattr("app.api.invoices.dispatch_outbox_event", lambda *args, **kwargs: None)
    acme_id, harbor_id = _add_vendors(api["app"])
    vendor_user, vendor_headers = _provision_vendor(api, harbor_id)

    upload = api["client"].post(
        "/api/v1/invoices/vendor/upload",
        headers={**vendor_headers, "Idempotency-Key": "vendor-upload-synthetic-001"},
        files={"file": ("synthetic-vendor-invoice.pdf", _synthetic_invoice_pdf(), "application/pdf")},
    )
    assert upload.status_code == 202, upload.text
    invoice_id = upload.json()["invoice_id"]
    job_id = upload.json()["job_id"]

    # Vendor users can submit, but the internal workspace and document routes remain denied.
    assert api["client"].get("/api/v1/invoices?limit=1", headers=vendor_headers).status_code == 403
    assert api["client"].get(f"/api/v1/invoices/{invoice_id}", headers=vendor_headers).status_code == 403
    assert api["client"].get(f"/api/v1/invoices/{invoice_id}/document", headers=vendor_headers).status_code == 403

    with api["app"].state.session_factory() as db:
        before = db.get(Invoice, invoice_id)
        assert before is not None
        assert before.source == "vendor_upload"
        assert before.vendor_id == harbor_id
        assert db.get(User, vendor_user["id"]).vendor_id == harbor_id

    run_processing_job(job_id, api["app"].state.session_factory, api["app"].state.storage_dir)

    with api["app"].state.session_factory() as db:
        invoice = db.get(Invoice, invoice_id)
        assert invoice is not None
        # Extraction identifies Acme, but must never overwrite the Harbor account binding.
        assert invoice.vendor_id == harbor_id
        assert invoice.vendor_id != acme_id
        assert invoice.workflow_status == "review_required"
        case = db.scalar(select(ReviewCase).where(ReviewCase.invoice_id == invoice_id))
        assert case is not None
        assert "vendor_identity_mismatch" in case.reason_codes
        assert db.scalar(select(Role.id).where(Role.name == "vendor")) is not None


def test_vendor_upload_status_is_bound_to_actor_and_vendor_scope(api, monkeypatch):
    monkeypatch.setattr(settings, "task_mode", "celery")
    monkeypatch.setattr("app.api.invoices.dispatch_outbox_event", lambda *_args, **_kwargs: None)
    acme_id, harbor_id = _add_vendors(api["app"])
    harbor_user, harbor_headers = _provision_vendor(api, harbor_id, email="harbor-status@test.local")
    _acme_user, acme_headers = _provision_vendor(api, acme_id, email="acme-status@test.local")
    key = "vendor-operation-status-001"
    upload = api["client"].post(
        "/api/v1/invoices/vendor/upload",
        headers={**harbor_headers, "Idempotency-Key": key},
        files={"file": ("synthetic-vendor-status.pdf", _synthetic_invoice_pdf(), "application/pdf")},
    )
    assert upload.status_code == 202, upload.text

    own_status = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**harbor_headers, "Idempotency-Key": key},
    )
    assert own_status.status_code == 200, own_status.text
    assert own_status.json()["status"] == "committed"
    assert own_status.json()["invoice_id"] == upload.json()["invoice_id"]
    assert own_status.json()["receipt_state"] == "committed"

    cross_vendor = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**acme_headers, "Idempotency-Key": key},
    )
    cross_actor_internal = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**api["admin"], "Idempotency-Key": key},
    )
    unknown = api["client"].get(
        "/api/v1/invoices/upload-status",
        headers={**acme_headers, "Idempotency-Key": "unseen-vendor-operation"},
    )
    assert cross_vendor.status_code == cross_actor_internal.status_code == unknown.status_code == 200
    assert cross_vendor.json() == unknown.json() == cross_actor_internal.json()
    assert cross_vendor.json()["status"] == "not_found"
    assert cross_vendor.json()["invoice_id"] is None
    assert harbor_user["id"] != _acme_user["id"]


def test_vendor_role_requires_exactly_one_active_vendor_binding(api):
    acme_id, _ = _add_vendors(api["app"])
    missing_binding = api["client"].post(
        "/api/v1/admin/users",
        headers=api["admin"],
        json={"email": "vendor-nobinding@test.local", "display_name": "No Binding", "password": "VendorPass123!", "roles": ["vendor"]},
    )
    assert missing_binding.status_code == 422

    mixed_roles = api["client"].post(
        "/api/v1/admin/users",
        headers=api["admin"],
        json={"email": "vendor-mixed@test.local", "display_name": "Mixed Roles", "password": "VendorPass123!", "roles": ["vendor", "reviewer"], "vendor_id": acme_id},
    )
    assert mixed_roles.status_code == 422

    with api["app"].state.session_factory() as db:
        inactive = Vendor(code="INACTIVE-SYN-3", name="Inactive Synthetic Supplier", is_active=False)
        db.add(inactive)
        db.commit()
        inactive_id = inactive.id
    inactive_vendor = api["client"].post(
        "/api/v1/admin/users",
        headers=api["admin"],
        json={"email": "vendor-inactive@test.local", "display_name": "Inactive Binding", "password": "VendorPass123!", "roles": ["vendor"], "vendor_id": inactive_id},
    )
    assert inactive_vendor.status_code == 422


def test_vendor_role_update_preserves_or_clears_binding_safely(api):
    acme_id, _ = _add_vendors(api["app"])
    payload, _headers = _provision_vendor(api, acme_id, email="vendor-role-update@test.local")

    preserved = api["client"].put(
        f"/api/v1/admin/users/{payload['id']}/roles",
        headers=api["admin"],
        json={"roles": ["vendor"]},
    )
    assert preserved.status_code == 200, preserved.text
    assert preserved.json()["vendor_id"] == acme_id

    cleared = api["client"].put(
        f"/api/v1/admin/users/{payload['id']}/roles",
        headers=api["admin"],
        json={"roles": ["auditor"]},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["vendor_id"] is None

    with api["app"].state.session_factory() as db:
        user = db.get(User, payload["id"])
        assert user is not None and user.vendor_id is None
