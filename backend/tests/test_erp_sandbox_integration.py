from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from app.integrations.erp_sandbox import (
    ERPContractError,
    ERPNotConfigured,
    ERPSyncConflict,
    EXTERNAL_SOURCE,
    apply_sandbox_records,
    fetch_invoice_status,
    fetch_sandbox_records,
)
from app.models import AuditEvent, GoodsReceipt, Invoice, PurchaseOrder, PurchaseOrderLine, Vendor


def _records():
    return {
        "vendors": [{
            "external_id": "vendor-x1", "code": "v-x1", "name": "Example Industrial",
            "tax_identifier": "TAX-123", "aliases": ["Example Ind"],
        }],
        "purchase_orders": [{
            "external_id": "po-x1", "number": "PO-X1", "vendor_external_id": "vendor-x1",
            "currency": "USD", "status": "open", "issue_date": "2026-01-05",
            "lines": [{
                "line_number": 1, "sku": "PART-1", "description": "Industrial Widget",
                "quantity": "10", "unit_price": "12.50", "tax_rate": "0.08",
            }],
        }],
        "goods_receipts": [{
            "external_id": "grn-x1", "number": "GRN-X1", "purchase_order_external_id": "po-x1",
            "received_date": "2026-01-07",
            "lines": [{
                "purchase_order_line_number": 1, "sku": "PART-1",
                "description": "Industrial Widget", "quantity": "8",
            }],
        }],
    }


def test_missing_erp_credentials_fail_before_transport_invocation():
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request))

    with pytest.raises(ERPNotConfigured, match="both be configured"):
        fetch_sandbox_records("http://localhost:9000", None, transport=transport)

    assert requests == []


def test_local_schema_compatibility_adds_nullable_erp_identity_columns_and_preserves_rows():
    from app.main import _ensure_local_additive_schema

    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE vendors (id VARCHAR(36) PRIMARY KEY, code VARCHAR(48), name VARCHAR(180))")
        connection.exec_driver_sql("CREATE TABLE purchase_orders (id VARCHAR(36) PRIMARY KEY, number VARCHAR(80))")
        connection.exec_driver_sql("CREATE TABLE goods_receipts (id VARCHAR(36) PRIMARY KEY, number VARCHAR(80))")
        connection.exec_driver_sql("CREATE TABLE processing_jobs (id VARCHAR(36) PRIMARY KEY, heartbeat_at DATETIME)")
        connection.exec_driver_sql("INSERT INTO vendors (id, code, name) VALUES ('vendor-legacy', 'LEGACY', 'Legacy Vendor')")
        connection.exec_driver_sql("INSERT INTO purchase_orders (id, number) VALUES ('po-legacy', 'PO-LEGACY')")
        connection.exec_driver_sql("INSERT INTO goods_receipts (id, number) VALUES ('gr-legacy', 'GR-LEGACY')")

    _ensure_local_additive_schema(engine)

    with engine.connect() as connection:
        for table_name in ("vendors", "purchase_orders", "goods_receipts"):
            columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
            assert {"external_source", "external_id"}.issubset(columns)
            assert connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table_name}").scalar_one() == 1
        vendor = connection.exec_driver_sql("SELECT code, name, external_source, external_id FROM vendors WHERE id='vendor-legacy'").one()
        assert tuple(vendor) == ("LEGACY", "Legacy Vendor", None, None)
        connection.exec_driver_sql("INSERT INTO vendors (id, code, name, external_source, external_id) VALUES ('vendor-new', 'NEW', 'New Vendor', 'erp', 'source-1')")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("INSERT INTO vendors (id, code, name, external_source, external_id) VALUES ('vendor-dup', 'DUP', 'Duplicate', 'erp', 'source-1')")
    engine.dispose()


def test_missing_status_credentials_fail_before_transport_invocation():
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request))

    with pytest.raises(ERPNotConfigured, match="both be configured"):
        fetch_invoice_status("http://localhost:9000", None, "INV-1", "V-1", transport=transport)

    assert requests == []


def test_sandbox_http_client_reads_all_pages_with_bearer_auth_and_bounds_redirects():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer sandbox-test-token"
        assert request.method == "GET"
        if request.url.path == "/api/v1/vendors":
            if request.url.params.get("cursor") == "vendor-next":
                return httpx.Response(200, json={"items": [], "next_cursor": None})
            return httpx.Response(200, json={"items": [{
                "external_id": "v-1", "code": "V-1", "name": "Sandbox Vendor",
            }], "next_cursor": "vendor-next"})
        if request.url.path == "/api/v1/purchase-orders":
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if request.url.path == "/api/v1/goods-receipts":
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(404)

    records = fetch_sandbox_records(
        "http://127.0.0.1:9001", "sandbox-test-token",
        transport=httpx.MockTransport(handler),
    )

    assert len(records["vendors"]) == 1
    assert records["vendors"][0]["code"] == "V-1"
    assert len(seen) == 4
    assert seen[0].url.params["limit"] == "100"
    assert seen[1].url.params["cursor"] == "vendor-next"


def test_sandbox_client_rejects_non_loopback_http_and_invalid_provider_contract():
    with pytest.raises(ERPNotConfigured, match="requires HTTPS"):
        fetch_sandbox_records("http://erp.example.com", "token")

    def bad_response(_request):
        return httpx.Response(200, content=json.dumps({"items": [{"unexpected": True}]}))

    with pytest.raises(ERPContractError, match="outside the documented schema"):
        fetch_sandbox_records(
            "http://localhost:9002", "token", transport=httpx.MockTransport(bad_response),
        )


def test_reference_sync_is_idempotent_and_preserves_external_provenance(api):
    app = api["app"]
    with app.state.session_factory() as db:
        created = apply_sandbox_records(db, _records())
        db.commit()

    assert {key: value["created"] for key, value in created.items()} == {
        "vendors": 1, "purchase_orders": 1, "goods_receipts": 1,
    }

    with app.state.session_factory() as db:
        repeated = apply_sandbox_records(db, _records())
        db.commit()
        vendor = db.scalar(select(Vendor).where(Vendor.external_id == "vendor-x1"))
        purchase_order = db.scalar(select(PurchaseOrder).where(PurchaseOrder.external_id == "po-x1"))
        receipt = db.scalar(select(GoodsReceipt).where(GoodsReceipt.external_id == "grn-x1"))

    assert {key: value["unchanged"] for key, value in repeated.items()} == {
        "vendors": 1, "purchase_orders": 1, "goods_receipts": 1,
    }
    assert vendor.external_source == EXTERNAL_SOURCE
    assert purchase_order.expected_total == 125
    assert receipt.lines[0].purchase_order_line_id == purchase_order.lines[0].id


def test_external_sync_rejects_and_rolls_back_conflict_with_manual_vendor(api):
    app = api["app"]
    with app.state.session_factory() as db:
        db.add(Vendor(code="V-X1", name="Locally Managed Name", aliases=[], risk_tier="elevated"))
        db.commit()

    with app.state.session_factory() as db:
        with pytest.raises(ERPSyncConflict, match="locally managed vendor"):
            apply_sandbox_records(db, _records())
        db.rollback()

    with app.state.session_factory() as db:
        vendor = db.scalar(select(Vendor).where(Vendor.code == "V-X1"))
        assert vendor.name == "Locally Managed Name"
        assert vendor.external_source is None
        assert db.scalar(select(PurchaseOrder).where(PurchaseOrder.number == "PO-X1")) is None


def test_admin_erp_route_is_admin_only_and_fails_closed_when_unconfigured(api, monkeypatch):
    from app.config import settings
    from app.integrations import erp_sandbox

    called = []
    monkeypatch.setattr(settings, "erp_sandbox_base_url", None)
    monkeypatch.setattr(settings, "erp_sandbox_token", None)
    monkeypatch.setattr(erp_sandbox.httpx, "Client", lambda *_args, **_kwargs: called.append(True))

    client = api["client"]
    auditor_response = client.post("/api/v1/admin/integrations/erp/sync", headers=api["auditor"])
    admin_response = client.post("/api/v1/admin/integrations/erp/sync", headers=api["admin"])

    assert auditor_response.status_code == 403
    assert admin_response.status_code == 503
    assert "no network request was made" in admin_response.json()["detail"]
    assert called == []


def test_admin_erp_route_commits_reference_rows_and_audits_counts(api, monkeypatch):
    from app.api import operations

    monkeypatch.setattr(operations, "fetch_sandbox_records", lambda *_args, **_kwargs: _records())
    response = api["client"].post("/api/v1/admin/integrations/erp/sync", headers=api["admin"])

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "read_only_reference_sync"
    assert payload["payment_execution"] is False
    assert payload["invoice_status_submission"] is False
    assert payload["counts"]["vendors"]["created"] == 1
    with api["app"].state.session_factory() as db:
        assert db.scalar(select(Vendor).where(Vendor.external_id == "vendor-x1")) is not None
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "erp.sandbox_reference_sync_completed"))
        assert event is not None
        assert event.details["counts"]["purchase_orders"]["created"] == 1
        assert "tax_identifier" not in str(event.details)


def test_admin_erp_route_rolls_back_all_rows_when_a_later_record_conflicts(api, monkeypatch):
    from app.api import operations

    with api["app"].state.session_factory() as db:
        vendor = Vendor(
            code="V-X1", name="Example Industrial", tax_identifier="TAX-123",
            aliases=["Example Ind"], risk_tier="elevated",
        )
        db.add(vendor)
        db.flush()
        purchase_order = PurchaseOrder(
            number="PO-X1", vendor_id=vendor.id, currency="USD", status="open",
            issue_date=date(2026, 1, 5), expected_total=Decimal("999.00"),
        )
        purchase_order.lines = [PurchaseOrderLine(
            line_number=1, sku="OTHER", description="Different locally managed item",
            quantity=Decimal("1"), unit_price=Decimal("999"), tax_rate=Decimal("0"),
        )]
        db.add(purchase_order)
        db.commit()
        vendor_id = vendor.id
        po_id = purchase_order.id

    monkeypatch.setattr(operations, "fetch_sandbox_records", lambda *_args, **_kwargs: _records())
    response = api["client"].post("/api/v1/admin/integrations/erp/sync", headers=api["admin"])

    assert response.status_code == 409
    with api["app"].state.session_factory() as db:
        vendor = db.get(Vendor, vendor_id)
        purchase_order = db.get(PurchaseOrder, po_id)
        assert vendor.external_source is None and vendor.external_id is None
        assert purchase_order.external_source is None and purchase_order.external_id is None
        assert purchase_order.lines[0].description == "Different locally managed item"
        assert db.scalar(select(GoodsReceipt).where(GoodsReceipt.number == "GRN-X1")) is None
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "erp.sandbox_reference_sync_completed")) is None


def test_provider_invoice_status_is_read_only_and_audited_without_local_workflow_change(api, monkeypatch):
    from app.api import operations

    with api["app"].state.session_factory() as db:
        vendor = Vendor(code="STATUS-V", name="Status Vendor", aliases=[])
        db.add(vendor)
        db.flush()
        invoice = Invoice(
            invoice_number="INV-STATUS-1", vendor_id=vendor.id, currency="USD",
            total_amount=Decimal("25.00"), processing_status="completed",
            workflow_status="review_required", source="upload", is_synthetic=True,
        )
        db.add(invoice)
        db.commit()
        invoice_id = invoice.id

    calls = []
    monkeypatch.setattr(operations, "fetch_invoice_status", lambda *_args, **_kwargs: calls.append(True) or {
        "found": True, "provider": EXTERNAL_SOURCE,
        "invoice_number": "INV-STATUS-1", "vendor_code": "STATUS-V",
        "status": "paid", "external_reference": "remote-ref-1",
        "updated_at": "2026-10-14T12:00:00+00:00",
        "note": "Provider-reported status only; no local state changed.",
        "payment_execution": False,
    })

    response = api["client"].get(f"/api/v1/admin/integrations/erp/invoices/{invoice_id}/status", headers=api["admin"])

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "paid"
    assert response.json()["payment_execution"] is False
    assert calls == [True]
    with api["app"].state.session_factory() as db:
        invoice = db.get(Invoice, invoice_id)
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "erp.invoice_status_read"))
        assert invoice.workflow_status == "review_required"
        assert invoice.processing_status == "completed"
        assert event is not None and event.details == {"provider": EXTERNAL_SOURCE, "found": True, "read_only": True}


def test_fetch_invoice_status_uses_query_params_and_returns_provider_record_only():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/invoice-status"
        assert request.url.params["invoice_number"] == "INV-500"
        assert request.url.params["vendor_code"] == "V-500"
        assert request.headers["Authorization"] == "Bearer read-only-token"
        return httpx.Response(200, json={"items": [{
            "invoice_number": "INV-500", "vendor_code": "V-500", "status": "PAID",
            "external_reference": "ERP-REF-500", "updated_at": "2026-10-14T12:00:00Z",
        }], "next_cursor": None})

    result = fetch_invoice_status(
        "http://127.0.0.1:9003", "read-only-token", "INV-500", "V-500",
        transport=httpx.MockTransport(handler),
    )

    assert result["found"] is True
    assert result["status"] == "paid"
    assert result["external_reference"] == "ERP-REF-500"
    assert result["payment_execution"] is False
    assert "does not change local workflow" in result["note"]
