from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import os
from threading import Barrier

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db import Base, create_database_engine, create_session_factory
from app.models import (
    ExtractionResult, GoodsReceipt, GoodsReceiptLine, Invoice, InvoiceLine,
    InvoiceMatchResult, PurchaseOrder, PurchaseOrderLine, Vendor,
)
from app.processing import _purchase_order_allocation_lock_statement, evaluate_invoice
from app.services.matching import TolerancePolicy, reconcile


def match_inputs(*, invoice_price="50.00", invoice_qty="2", received_qty="2", invoice_subtotal=None, invoice_total=None):
    unit_price = Decimal(invoice_price)
    quantity = Decimal(invoice_qty)
    line_total = unit_price * quantity
    subtotal = Decimal(invoice_subtotal) if invoice_subtotal is not None else line_total
    total = Decimal(invoice_total) if invoice_total is not None else subtotal
    invoice = {
        "vendor_id": "vendor-1", "currency": "USD", "purchase_order_number": "PO-1",
        "lines": [{"sku": "SKU-1", "description": "Precision widget", "quantity": str(quantity), "unit_price": str(unit_price), "line_total": str(line_total)}],
        "subtotal": str(subtotal), "tax_amount": "0.00", "shipping_amount": "0.00", "discount_amount": "0.00", "total_amount": str(total),
    }
    purchase_order = {
        "id": "po-1", "number": "PO-1", "vendor_id": "vendor-1", "currency": "USD",
        "lines": [{"id": "po-line-1", "line_number": 1, "sku": "SKU-1", "description": "Precision widget", "quantity": "2", "unit_price": "50.00", "tax_rate": "0.00"}],
    }
    receipts = [{"number": "GRN-1", "lines": [{"purchase_order_line_id": "po-line-1", "sku": "SKU-1", "description": "Precision widget", "quantity": str(received_qty)}]}]
    return invoice, purchase_order, receipts


def test_small_nonzero_price_variance_is_preserved_and_explained():
    invoice, po, receipts = match_inputs(invoice_price="50.01")
    result = reconcile(invoice, po, receipts, policy=TolerancePolicy(Decimal("0.05"), Decimal("0.005"), "stricter"))
    assert result["outcome"] == "MATCHED_WITHIN_TOLERANCE"
    assert result["line_results"][0]["status"] == "within_tolerance"
    variance = next(item for item in result["discrepancies"] if item["type"] == "unit_price_within_tolerance")
    assert variance["expected"] == "50.00"
    assert variance["actual"] == "50.01"
    assert variance["difference"] == "0.01"


def test_financial_variance_takes_priority_over_partial_receipt():
    invoice, po, receipts = match_inputs(invoice_price="55.00", received_qty="1")
    result = reconcile(invoice, po, receipts)
    assert result["partial_receipt"] is True
    assert result["outcome"] == "MISMATCH"
    assert result["financial_exposure"] == "60.00"  # $10 price delta on two units plus $50 invoiced beyond receipt


def test_partial_delivery_is_not_labeled_fraud_or_financial_mismatch():
    invoice, po, receipts = match_inputs(invoice_qty="1", received_qty="1")
    result = reconcile(invoice, po, receipts)
    assert result["outcome"] == "PARTIAL_MATCH"
    assert result["partial_receipt"] is True
    assert result["discrepancies"] == []


def test_missing_purchase_order_is_explicit_exception():
    invoice, _, _ = match_inputs()
    result = reconcile(invoice, None, [])
    assert result["outcome"] == "MISSING_PO"
    assert result["discrepancies"][0]["type"] == "missing_po"
    assert "not a fraud conclusion" in result["explanation"]


def test_cumulative_invoices_cannot_exceed_ordered_or_received_quantity():
    invoice, po, receipts = match_inputs(invoice_qty="1", received_qty="2")
    result = reconcile(invoice, po, receipts, previous_invoice_quantities={1: "2"})
    discrepancy_types = {item["type"] for item in result["discrepancies"]}
    assert result["outcome"] == "MISMATCH"
    assert "cumulative_quantity_above_ordered" in discrepancy_types
    assert "cumulative_quantity_above_received" in discrepancy_types
    # The same excess is not counted twice when it violates both constraints.
    assert result["financial_exposure"] == "50.00"


def test_partial_invoice_tax_uses_current_invoice_quantity_not_all_received_goods():
    invoice, po, receipts = match_inputs(invoice_qty="1", received_qty="2", invoice_total="55.00")
    po["lines"][0]["tax_rate"] = "0.10"
    invoice["tax_amount"] = "5.00"
    result = reconcile(invoice, po, receipts)
    assert result["outcome"] == "MATCHED"
    assert not any(item["type"] == "tax_variance" for item in result["discrepancies"])


def test_closed_po_and_unallocated_freight_are_explicit_exceptions():
    invoice, po, receipts = match_inputs()
    po["status"] = "closed"
    invoice["shipping_amount"] = "10.00"
    invoice["total_amount"] = "110.00"
    result = reconcile(invoice, po, receipts)
    discrepancy_types = {item["type"] for item in result["discrepancies"]}
    assert result["outcome"] == "MISMATCH"
    assert "po_closed" in discrepancy_types
    assert "freight_unallocated" in discrepancy_types


def test_unresolved_invoice_vendor_cannot_be_automatically_matched():
    invoice, po, receipts = match_inputs()
    invoice["vendor_id"] = None
    result = reconcile(invoice, po, receipts)
    assert result["outcome"] == "MANUAL_REVIEW"
    assert any(item["type"] == "vendor_identity_unresolved" for item in result["discrepancies"])


def test_receipt_quantity_is_not_reused_across_duplicate_sku_po_lines():
    invoice = {
        "vendor_id": "vendor-1", "currency": "USD", "subtotal": "200.00", "tax_amount": "0.00",
        "shipping_amount": "0.00", "discount_amount": "0.00", "total_amount": "200.00",
        "lines": [
            {"sku": "DUP-1", "description": "Repeated component", "quantity": "2", "unit_price": "50.00", "line_total": "100.00"},
            {"sku": "DUP-1", "description": "Repeated component", "quantity": "2", "unit_price": "50.00", "line_total": "100.00"},
        ],
    }
    po = {
        "number": "PO-DUP", "vendor_id": "vendor-1", "currency": "USD",
        "lines": [
            {"id": "line-1", "line_number": 1, "sku": "DUP-1", "description": "Repeated component", "quantity": "2", "unit_price": "50.00", "tax_rate": "0"},
            {"id": "line-2", "line_number": 2, "sku": "DUP-1", "description": "Repeated component", "quantity": "2", "unit_price": "50.00", "tax_rate": "0"},
        ],
    }
    receipts = [{"number": "GRN-DUP", "lines": [{"purchase_order_line_id": "line-1", "sku": "DUP-1", "description": "Repeated component", "quantity": "2"}]}]
    result = reconcile(invoice, po, receipts)
    assert result["outcome"] == "MISMATCH"
    assert result["line_results"][1]["received_quantity"] == "0"
    assert any(item["type"] == "quantity_above_received" and item["line"] == 2 for item in result["discrepancies"])


def test_unlinked_receipt_with_ambiguous_duplicate_sku_requires_manual_review():
    invoice, po, receipts = match_inputs()
    po["lines"].append({**po["lines"][0], "id": "line-2", "line_number": 2})
    receipts[0]["lines"][0].pop("purchase_order_line_id")
    result = reconcile(invoice, po, receipts)
    assert any(item["type"] == "ambiguous_receipt_line" for item in result["discrepancies"])
    assert result["outcome"] in {"MISMATCH", "MANUAL_REVIEW"}


def test_allocation_lock_statement_emits_postgresql_for_update():
    statement = _purchase_order_allocation_lock_statement("po-allocation-test")
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    assert "purchase_orders.id" in compiled


def test_concurrent_invoice_analysis_cannot_double_consume_po_quantity(tmp_path):
    """A synthetic integration regression for concurrent same-PO matching.

    Uses SQLite by default. CI may provide a separately migrated, disposable
    PostgreSQL URL through INVOICEGUARD_TEST_ALLOCATION_DATABASE_URL to exercise
    real SELECT FOR UPDATE locking and transaction isolation.
    """
    allocation_url = os.getenv("INVOICEGUARD_TEST_ALLOCATION_DATABASE_URL")
    if not allocation_url:
        allocation_url = f"sqlite:///{tmp_path / 'allocation-concurrency.db'}"
    engine = create_database_engine(allocation_url)
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    try:
        with sessions() as db:
            vendor = Vendor(code="ALLOC-SYNTH-01", name="Synthetic Allocation Vendor")
            db.add(vendor)
            db.flush()
            po = PurchaseOrder(
                number="PO-ALLOC-SYNTH-01", vendor_id=vendor.id, currency="USD",
                issue_date=date(2026, 10, 1), expected_total=Decimal("100.00"),
            )
            db.add(po)
            db.flush()
            po_line = PurchaseOrderLine(
                purchase_order_id=po.id, line_number=1, sku="ALLOC-1",
                description="Synthetic allocation widget", quantity=Decimal("1"),
                unit_price=Decimal("100.00"), tax_rate=Decimal("0"),
            )
            db.add(po_line)
            db.flush()
            grn = GoodsReceipt(
                number="GRN-ALLOC-SYNTH-01", purchase_order_id=po.id,
                received_date=date(2026, 10, 2), status="received",
            )
            db.add(grn)
            db.flush()
            db.add(GoodsReceiptLine(
                goods_receipt_id=grn.id, purchase_order_line_id=po_line.id,
                sku="ALLOC-1", description="Synthetic allocation widget",
                quantity=Decimal("1"),
            ))
            invoice_ids = []
            for index in (1, 2):
                invoice = Invoice(
                    invoice_number=f"INV-ALLOC-SYNTH-{index}", vendor_id=vendor.id,
                    purchase_order_id=po.id, currency="USD", issue_date=date(2026, 10, 3),
                    subtotal=Decimal("100.00"), tax_amount=Decimal("0.00"),
                    total_amount=Decimal("100.00"), processing_status="completed",
                    workflow_status="processing", extraction_confidence=0.99,
                    source="synthetic", is_synthetic=True,
                )
                db.add(invoice)
                db.flush()
                db.add_all([
                    InvoiceLine(
                        invoice_id=invoice.id, line_number=1, sku="ALLOC-1",
                        description="Synthetic allocation widget", quantity=Decimal("1"),
                        unit_price=Decimal("100.00"), tax_rate=Decimal("0"),
                        tax_amount=Decimal("0.00"), line_total=Decimal("100.00"),
                    ),
                    ExtractionResult(
                        invoice_id=invoice.id, raw_text="synthetic test data",
                        extracted_fields={"currency": {"normalized_value": "USD", "confidence": 0.99}},
                        tokens=[], validation_errors=[], engine_name="synthetic-test",
                        engine_version="1", extraction_version=1,
                    ),
                ])
                invoice_ids.append(invoice.id)
            db.commit()

        # Release both workers together; separate file-backed SQLite sessions exercise
        # the same BEGIN IMMEDIATE writer serialization configured for local operation.
        gate = Barrier(2)

        def evaluate(invoice_id):
            gate.wait(timeout=10)
            with sessions() as db:
                invoice = db.get(Invoice, invoice_id)
                result = evaluate_invoice(db, invoice)
                db.commit()
                return result["match"]["outcome"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(evaluate, invoice_ids))
        assert sorted(outcomes) == ["MATCHED", "MISMATCH"]
        with sessions() as db:
            persisted = db.scalars(
                select(InvoiceMatchResult).order_by(InvoiceMatchResult.evaluated_at)
            ).all()
            assert sorted(row.outcome for row in persisted) == ["MATCHED", "MISMATCH"]
            over_allocated = next(row for row in persisted if row.outcome == "MISMATCH")
            assert any(
                discrepancy["type"] == "cumulative_quantity_above_ordered"
                for discrepancy in over_allocated.details["discrepancies"]
            )
    finally:
        engine.dispose()
