from __future__ import annotations

import ipaddress
import json
import re
import time
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GoodsReceipt, GoodsReceiptLine, Invoice, PurchaseOrder, PurchaseOrderLine, Vendor


EXTERNAL_SOURCE = "erp-rest-sandbox-v1"
PAGE_SIZE = 100
MAX_PAGES_PER_RESOURCE = 20
MAX_RECORDS_PER_RESOURCE = PAGE_SIZE * MAX_PAGES_PER_RESOURCE
MAX_RESPONSE_BYTES = 2_000_000
MAX_SYNC_SECONDS = 120


class ERPIntegrationError(RuntimeError):
    """Safe base error for provider/configuration failures."""


class ERPNotConfigured(ERPIntegrationError):
    pass


class ERPProviderError(ERPIntegrationError):
    pass


class ERPContractError(ERPIntegrationError):
    pass


class ERPSyncConflict(ERPIntegrationError):
    pass


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class VendorRecord(_ContractModel):
    external_id: str = Field(min_length=1, max_length=160)
    code: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=2, max_length=180)
    tax_identifier: str | None = Field(default=None, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("code")
    @classmethod
    def code_upper(cls, value: str) -> str:
        return value.upper()


class PurchaseOrderLineRecord(_ContractModel):
    line_number: int = Field(ge=1)
    sku: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)


class PurchaseOrderRecord(_ContractModel):
    external_id: str = Field(min_length=1, max_length=160)
    number: str = Field(min_length=2, max_length=80)
    vendor_external_id: str = Field(min_length=1, max_length=160)
    currency: str = Field(min_length=3, max_length=3)
    status: Literal["open", "closed", "cancelled"] = "open"
    issue_date: date
    lines: list[PurchaseOrderLineRecord] = Field(min_length=1, max_length=500)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str) -> str:
        normalized = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("currency must be a three-letter uppercase code")
        return normalized

    @model_validator(mode="after")
    def unique_lines(self):
        line_numbers = [line.line_number for line in self.lines]
        if len(line_numbers) != len(set(line_numbers)):
            raise ValueError("purchase-order line numbers must be unique")
        return self


class ReceiptLineRecord(_ContractModel):
    purchase_order_line_number: int | None = Field(default=None, ge=1)
    sku: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    quantity: Decimal = Field(gt=0)


class GoodsReceiptRecord(_ContractModel):
    external_id: str = Field(min_length=1, max_length=160)
    number: str = Field(min_length=2, max_length=80)
    purchase_order_external_id: str = Field(min_length=1, max_length=160)
    received_date: date
    lines: list[ReceiptLineRecord] = Field(min_length=1, max_length=500)


class ERPInvoiceStatusRecord(_ContractModel):
    invoice_number: str = Field(min_length=1, max_length=100)
    vendor_code: str = Field(min_length=2, max_length=48)
    status: str = Field(min_length=1, max_length=40)
    external_reference: str | None = Field(default=None, max_length=160)
    updated_at: datetime | None = None

    @field_validator("vendor_code")
    @classmethod
    def vendor_code_upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("status")
    @classmethod
    def safe_status_label(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9 _.-]{1,40}", normalized):
            raise ValueError("status must be a simple label")
        return normalized


RESOURCE_CONTRACTS = {
    "vendors": ("api/v1/vendors", VendorRecord),
    "purchase_orders": ("api/v1/purchase-orders", PurchaseOrderRecord),
    "goods_receipts": ("api/v1/goods-receipts", GoodsReceiptRecord),
}


def _validated_base_url(raw: str | None, token: str | None) -> tuple[str, str]:
    base_url = (raw or "").strip()
    bearer = (token or "").strip()
    if not base_url or not bearer:
        raise ERPNotConfigured("ERP sandbox URL and token must both be configured before synchronization.")
    if len(bearer) > 4096 or any(char in bearer for char in "\r\n"):
        raise ERPNotConfigured("ERP sandbox token configuration is invalid.")
    try:
        parts = urlsplit(base_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise ERPNotConfigured("ERP sandbox URL configuration is invalid.") from None
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise ERPNotConfigured("ERP sandbox URL must not contain user-info, query parameters, or a fragment.")
    try:
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ERPNotConfigured("ERP sandbox requires HTTPS; plain HTTP is allowed only for a loopback mock server.")
    if port is not None and not 1 <= port <= 65535:
        raise ERPNotConfigured("ERP sandbox URL port is invalid.")
    path = parts.path.rstrip("/")
    normalized = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return normalized, bearer


def _read_json_page(client: httpx.Client, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        with client.stream("GET", endpoint, params=params) as response:
            if response.status_code != 200:
                raise ERPProviderError(f"ERP sandbox returned HTTP {response.status_code} for a read request.")
            declared_size = response.headers.get("content-length")
            if declared_size:
                try:
                    if int(declared_size) > MAX_RESPONSE_BYTES:
                        raise ERPContractError("ERP sandbox page exceeded the 2 MB response limit.")
                except ValueError:
                    raise ERPContractError("ERP sandbox returned an invalid Content-Length header.") from None
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ERPContractError("ERP sandbox page exceeded the 2 MB response limit.")
    except httpx.TimeoutException:
        raise ERPProviderError("ERP sandbox request timed out.") from None
    except httpx.RequestError:
        raise ERPProviderError("ERP sandbox request failed before a complete response was received.") from None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ERPContractError("ERP sandbox returned a non-JSON response.") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ERPContractError("ERP sandbox page must contain an 'items' array.")
    return data


def fetch_sandbox_records(
    base_url: str | None,
    token: str | None,
    *,
    timeout_seconds: int = 15,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch and validate all three read-only ERP resources under a bounded contract."""
    normalized_url, bearer = _validated_base_url(base_url, token)
    timeout_seconds = max(1, min(60, int(timeout_seconds)))
    timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, float(timeout_seconds)))
    output: dict[str, list[dict[str, Any]]] = {}
    deadline = time.monotonic() + MAX_SYNC_SECONDS
    try:
        with httpx.Client(
            base_url=normalized_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json", "User-Agent": "LedgerLens-ERP-Sandbox/1"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        ) as client:
            for resource, (endpoint, model) in RESOURCE_CONTRACTS.items():
                items: list[dict[str, Any]] = []
                cursor: str | None = None
                seen_cursors: set[str] = set()
                for _page_number in range(MAX_PAGES_PER_RESOURCE):
                    if time.monotonic() >= deadline:
                        raise ERPProviderError(f"ERP sandbox synchronization exceeded the {MAX_SYNC_SECONDS}-second total time limit.")
                    params: dict[str, Any] = {"limit": PAGE_SIZE}
                    if cursor:
                        params["cursor"] = cursor
                    payload = _read_json_page(client, endpoint, params)
                    page_items = payload["items"]
                    if len(page_items) > PAGE_SIZE:
                        raise ERPContractError(f"ERP sandbox '{resource}' page exceeded the {PAGE_SIZE}-record limit.")
                    try:
                        items.extend(model.model_validate(item).model_dump(mode="python") for item in page_items)
                    except ValidationError:
                        raise ERPContractError(f"ERP sandbox '{resource}' contains a record outside the documented schema.") from None
                    if len(items) > MAX_RECORDS_PER_RESOURCE:
                        raise ERPContractError(f"ERP sandbox '{resource}' exceeded the bounded import size.")
                    next_cursor = payload.get("next_cursor")
                    if next_cursor is None:
                        break
                    if not isinstance(next_cursor, str) or not next_cursor.strip() or len(next_cursor) > 200 or next_cursor in seen_cursors:
                        raise ERPContractError(f"ERP sandbox '{resource}' returned an invalid or repeated pagination cursor.")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    raise ERPContractError(f"ERP sandbox '{resource}' exceeded the {MAX_PAGES_PER_RESOURCE}-page limit.")
                output[resource] = items
    except ERPIntegrationError:
        raise
    except httpx.HTTPError:
        raise ERPProviderError("ERP sandbox transport failed.") from None
    return output


def fetch_invoice_status(
    base_url: str | None,
    token: str | None,
    invoice_number: str,
    vendor_code: str,
    *,
    timeout_seconds: int = 15,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Read, but never persist or act on, one provider-reported invoice status."""
    normalized_url, bearer = _validated_base_url(base_url, token)
    timeout_seconds = max(1, min(60, int(timeout_seconds)))
    timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, float(timeout_seconds)))
    try:
        with httpx.Client(
            base_url=normalized_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json", "User-Agent": "LedgerLens-ERP-Sandbox/1"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        ) as client:
            payload = _read_json_page(
                client, "api/v1/invoice-status",
                {"invoice_number": invoice_number, "vendor_code": vendor_code},
            )
    except ERPIntegrationError:
        raise
    except httpx.HTTPError:
        raise ERPProviderError("ERP sandbox status request failed.") from None
    items = payload["items"]
    if payload.get("next_cursor") is not None or len(items) > 1:
        raise ERPContractError("ERP sandbox invoice-status response must contain zero or one result and no pagination cursor.")
    if not items:
        return {
            "found": False, "provider": EXTERNAL_SOURCE,
            "invoice_number": invoice_number, "vendor_code": vendor_code.upper(),
            "status": None, "external_reference": None, "updated_at": None,
            "note": "No matching provider status was returned; no local workflow state was changed.",
            "payment_execution": False,
        }
    try:
        record = ERPInvoiceStatusRecord.model_validate(items[0])
    except ValidationError:
        raise ERPContractError("ERP sandbox invoice-status record is outside the documented schema.") from None
    if record.invoice_number != invoice_number or record.vendor_code != vendor_code.upper():
        raise ERPContractError("ERP sandbox invoice-status record did not match the requested invoice and vendor.")
    return {
        "found": True, "provider": EXTERNAL_SOURCE,
        "invoice_number": record.invoice_number, "vendor_code": record.vendor_code,
        "status": record.status, "external_reference": record.external_reference,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "note": "Provider-reported status only; this lookup does not change local workflow, authorize approval, or execute payment.",
        "payment_execution": False,
    }


def _vendor_matches(vendor: Vendor, record: VendorRecord) -> bool:
    return (
        vendor.code == record.code
        and vendor.name == record.name
        and vendor.tax_identifier == record.tax_identifier
        and list(vendor.aliases or []) == record.aliases
    )


def _po_line_signature(lines: list[PurchaseOrderLine] | list[PurchaseOrderLineRecord]) -> list[tuple[Any, ...]]:
    return sorted((
        int(line.line_number), line.sku, line.description,
        Decimal(str(line.quantity)), Decimal(str(line.unit_price)), Decimal(str(line.tax_rate)),
    ) for line in lines)


def _po_values_match(po: PurchaseOrder, record: PurchaseOrderRecord, vendor_id: str) -> bool:
    return (
        po.number == record.number
        and po.vendor_id == vendor_id
        and po.currency == record.currency
        and po.status == record.status
        and po.issue_date == record.issue_date
        and po.expected_total == _po_expected_total(record)
        and _po_line_signature(po.lines) == _po_line_signature(record.lines)
    )


def _po_expected_total(record: PurchaseOrderRecord) -> Decimal:
    return sum((line.quantity * line.unit_price for line in record.lines), Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _receipt_signature(receipt: GoodsReceipt) -> list[tuple[Any, ...]]:
    rows = [
        (line.purchase_order_line_id, line.sku, line.description, Decimal(str(line.quantity)))
        for line in receipt.lines
    ]
    return sorted(rows, key=lambda item: (str(item[0] or ""), str(item[1] or ""), item[2], item[3]))


def _receipt_record_signature(record: GoodsReceiptRecord, po: PurchaseOrder) -> list[tuple[Any, ...]]:
    line_by_number = {line.line_number: line for line in po.lines}
    signature = []
    for line in record.lines:
        po_line = line_by_number.get(line.purchase_order_line_number) if line.purchase_order_line_number is not None else None
        signature.append((po_line.id if po_line else None, line.sku, line.description, line.quantity))
    return sorted(signature, key=lambda item: (str(item[0] or ""), str(item[1] or ""), item[2], item[3]))


def _new_receipt_lines(record: GoodsReceiptRecord, po: PurchaseOrder) -> list[GoodsReceiptLine]:
    line_by_number = {line.line_number: line for line in po.lines}
    output = []
    for line in record.lines:
        po_line = line_by_number.get(line.purchase_order_line_number) if line.purchase_order_line_number is not None else None
        if line.purchase_order_line_number is not None and po_line is None:
            raise ERPContractError("ERP goods receipt references a purchase-order line that does not exist.")
        if po_line and line.sku and po_line.sku and line.sku != po_line.sku:
            raise ERPContractError("ERP goods receipt SKU conflicts with its referenced purchase-order line.")
        output.append(GoodsReceiptLine(
            purchase_order_line_id=po_line.id if po_line else None,
            sku=line.sku, description=line.description, quantity=line.quantity,
        ))
    return output


def apply_sandbox_records(
    db: Session,
    records: dict[str, list[dict[str, Any]]],
    *,
    source: str = EXTERNAL_SOURCE,
) -> dict[str, dict[str, int]]:
    """Atomically upsert source-owned reference rows; reject collisions with manual data."""
    try:
        vendors = [VendorRecord.model_validate(item) for item in records.get("vendors", [])]
        purchase_orders = [PurchaseOrderRecord.model_validate(item) for item in records.get("purchase_orders", [])]
        goods_receipts = [GoodsReceiptRecord.model_validate(item) for item in records.get("goods_receipts", [])]
    except (ValidationError, TypeError):
        raise ERPContractError("ERP sandbox records failed local schema validation.") from None
    for resource, items in (("vendors", vendors), ("purchase_orders", purchase_orders), ("goods_receipts", goods_receipts)):
        external_ids = [item.external_id for item in items]
        natural_ids = [item.code if resource == "vendors" else item.number for item in items]
        if len(external_ids) != len(set(external_ids)) or len(natural_ids) != len(set(natural_ids)):
            raise ERPContractError(f"ERP sandbox '{resource}' contains duplicate external or business identifiers.")

    counts = {
        name: {"created": 0, "updated": 0, "unchanged": 0, "linked": 0}
        for name in ("vendors", "purchase_orders", "goods_receipts")
    }
    vendor_ids_by_external: dict[str, str] = {}

    for record in vendors:
        existing = db.scalar(select(Vendor).where(Vendor.external_source == source, Vendor.external_id == record.external_id))
        natural = db.scalar(select(Vendor).where(Vendor.code == record.code))
        if existing and natural and natural.id != existing.id:
            raise ERPSyncConflict(f"ERP vendor code {record.code!r} conflicts with another local record; no records were changed.")
        if existing is None and natural is not None:
            if not _vendor_matches(natural, record):
                raise ERPSyncConflict(f"ERP vendor code {record.code!r} conflicts with a locally managed vendor; no records were changed.")
            natural.external_source, natural.external_id = source, record.external_id
            existing = natural
            counts["vendors"]["linked"] += 1
        elif existing is None:
            existing = Vendor(
                code=record.code, name=record.name, tax_identifier=record.tax_identifier,
                aliases=record.aliases, risk_tier="standard", external_source=source, external_id=record.external_id,
            )
            db.add(existing)
            counts["vendors"]["created"] += 1
        else:
            changed = not _vendor_matches(existing, record)
            existing.code = record.code
            existing.name = record.name
            existing.tax_identifier = record.tax_identifier
            existing.aliases = record.aliases
            if changed:
                counts["vendors"]["updated"] += 1
            else:
                counts["vendors"]["unchanged"] += 1
        existing.external_source, existing.external_id = source, record.external_id
        vendor_ids_by_external[record.external_id] = existing.id
    db.flush()

    po_ids_by_external: dict[str, str] = {}
    for record in purchase_orders:
        vendor_id = vendor_ids_by_external.get(record.vendor_external_id)
        if vendor_id is None:
            vendor = db.scalar(select(Vendor).where(Vendor.external_source == source, Vendor.external_id == record.vendor_external_id))
            vendor_id = vendor.id if vendor else None
        if vendor_id is None:
            raise ERPContractError("ERP purchase order references a vendor absent from the sync and local source mapping.")
        existing = db.scalar(select(PurchaseOrder).where(PurchaseOrder.external_source == source, PurchaseOrder.external_id == record.external_id))
        natural = db.scalar(select(PurchaseOrder).where(PurchaseOrder.number == record.number))
        if existing and natural and natural.id != existing.id:
            raise ERPSyncConflict(f"ERP purchase-order number {record.number!r} conflicts with another local record; no records were changed.")
        if existing is None and natural is not None:
            if not _po_values_match(natural, record, vendor_id):
                raise ERPSyncConflict(f"ERP purchase-order number {record.number!r} conflicts with a locally managed order; no records were changed.")
            natural.external_source, natural.external_id = source, record.external_id
            existing = natural
            counts["purchase_orders"]["linked"] += 1
        elif existing is None:
            existing = PurchaseOrder(
                number=record.number, vendor_id=vendor_id, currency=record.currency,
                status=record.status, issue_date=record.issue_date,
                expected_total=_po_expected_total(record), external_source=source, external_id=record.external_id,
            )
            existing.lines = [PurchaseOrderLine(**line.model_dump()) for line in record.lines]
            db.add(existing)
            counts["purchase_orders"]["created"] += 1
        else:
            changed = not _po_values_match(existing, record, vendor_id)
            if changed:
                has_invoices = db.scalar(select(Invoice.id).where(Invoice.purchase_order_id == existing.id).limit(1)) is not None
                has_receipts = db.scalar(select(GoodsReceipt.id).where(GoodsReceipt.purchase_order_id == existing.id).limit(1)) is not None
                if has_invoices or has_receipts:
                    raise ERPSyncConflict(f"ERP purchase order {record.number!r} changed after local invoices or receipts referenced it; manual reconciliation is required.")
                existing.number = record.number
                existing.vendor_id = vendor_id
                existing.currency = record.currency
                existing.status = record.status
                existing.issue_date = record.issue_date
                existing.expected_total = _po_expected_total(record)
                existing.lines.clear()
                existing.lines.extend(PurchaseOrderLine(**line.model_dump()) for line in record.lines)
                counts["purchase_orders"]["updated"] += 1
            else:
                counts["purchase_orders"]["unchanged"] += 1
            existing.external_source, existing.external_id = source, record.external_id
        db.flush()
        po_ids_by_external[record.external_id] = existing.id

    for record in goods_receipts:
        po_id = po_ids_by_external.get(record.purchase_order_external_id)
        if po_id is None:
            po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.external_source == source, PurchaseOrder.external_id == record.purchase_order_external_id))
        else:
            po = db.get(PurchaseOrder, po_id)
        if po is None:
            raise ERPContractError("ERP goods receipt references a purchase order absent from the sync and local source mapping.")
        existing = db.scalar(select(GoodsReceipt).where(GoodsReceipt.external_source == source, GoodsReceipt.external_id == record.external_id))
        natural = db.scalar(select(GoodsReceipt).where(GoodsReceipt.number == record.number))
        if existing and natural and natural.id != existing.id:
            raise ERPSyncConflict(f"ERP goods receipt number {record.number!r} conflicts with another local record; no records were changed.")
        expected_signature = _receipt_record_signature(record, po)
        if existing is None and natural is not None:
            same = (
                natural.purchase_order_id == po.id
                and natural.received_date == record.received_date
                and _receipt_signature(natural) == expected_signature
            )
            if not same:
                raise ERPSyncConflict(f"ERP goods receipt number {record.number!r} conflicts with a locally managed receipt; no records were changed.")
            natural.external_source, natural.external_id = source, record.external_id
            existing = natural
            counts["goods_receipts"]["linked"] += 1
        elif existing is None:
            existing = GoodsReceipt(
                number=record.number, purchase_order_id=po.id, received_date=record.received_date,
                status="received", external_source=source, external_id=record.external_id,
            )
            existing.lines = _new_receipt_lines(record, po)
            db.add(existing)
            counts["goods_receipts"]["created"] += 1
        else:
            same = (
                existing.number == record.number
                and existing.purchase_order_id == po.id
                and existing.received_date == record.received_date
                and _receipt_signature(existing) == expected_signature
            )
            if not same:
                raise ERPSyncConflict(f"ERP goods receipt {record.number!r} changed; receipts are immutable in this sync path and require manual reconciliation.")
            counts["goods_receipts"]["unchanged"] += 1
            existing.external_source, existing.external_id = source, record.external_id
        db.flush()

    return counts
