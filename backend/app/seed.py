from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_audit_event
from app.config import PROJECT_ROOT, settings
from app.db import create_database_engine, create_session_factory
from app.models import (
    ApplicationSetting, DocumentFile, Employee, GoodsReceipt, GoodsReceiptLine,
    Invoice, ModelVersion, ProcessingJob, PurchaseOrder, PurchaseOrderLine,
    Role, User, Vendor, new_id,
)
from app.security import hash_password


DEMO_USERS = [
    ("admin@demo.local", "Avery Morgan", "admin", "DemoPass123!"),
    ("reviewer@demo.local", "Jordan Patel", "reviewer", "ReviewPass123!"),
    ("clerk@demo.local", "Casey Reed", "ap_clerk", "ClerkPass123!"),
    ("auditor@demo.local", "Taylor Nguyen", "auditor", "AuditPass123!"),
]


def _token(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def seed_demo_data(session_factory, storage_dir: Path | None = None) -> list[str]:
    """Seed clearly labeled synthetic reference records once; never seeds in production by default."""
    storage_dir = (storage_dir or settings.storage_path).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    docs_root = PROJECT_ROOT / "data" / "demo_docs"
    with session_factory() as db:
        if db.scalar(select(User.id).limit(1)):
            return []
        role_descriptions = {
            "admin": "Local development administrator",
            "reviewer": "Review exceptions and approve or reject cases",
            "ap_clerk": "Process invoice uploads and correct extraction fields",
            "approver": "Independently approve or reject non-executable payment proposals",
            "auditor": "Read-only operational and audit access",
        }
        roles = {}
        for role_name, description in role_descriptions.items():
            roles[role_name] = db.scalar(select(Role).where(Role.name == role_name)) or Role(name=role_name, description=description)
        db.add_all(roles.values())
        db.flush()
        for email, display_name, role_name, password in DEMO_USERS:
            db.add(User(email=email, display_name=display_name, password_hash=hash_password(password), roles=[roles[role_name]]))

        acme = Vendor(code="ACME-001", name="Acme Systems LLC", aliases=["Acme Systems", "Acme"], tax_identifier="12-3456789", risk_tier="standard", address_token=_token("acme-demo-address"), bank_token=_token("acme-demo-bank"))
        harbor = Vendor(code="HBR-002", name="Harbor Industrial Supply", aliases=["Harbor Industrial"], tax_identifier="98-7654321", risk_tier="standard", address_token=_token("harbor-demo-address"), bank_token=_token("harbor-demo-bank"))
        northstar = Vendor(code="NST-003", name="Northstar Office Supply", aliases=["Northstar Office"], tax_identifier="44-1234567", risk_tier="standard", address_token=_token("northstar-demo-address"), bank_token=_token("northstar-demo-bank"))
        nova = Vendor(code="NVL-004", name="New Vista Logistics", aliases=["New Vista"], tax_identifier="55-5555555", risk_tier="elevated", address_token=_token("nova-demo-address"), bank_token=_token("demo-shared-bank-token"))
        db.add_all([acme, harbor, northstar, nova])
        employee = Employee(employee_code="EMP-DEMO-17", display_name="Demo employee record", email="employee.demo@example.invalid", address_token=_token("employee-demo-address"), bank_token=_token("demo-shared-bank-token"))
        db.add(employee)
        db.flush()

        po_acme = PurchaseOrder(number="PO-2026-104", vendor_id=acme.id, currency="USD", status="open", issue_date=date(2026, 9, 1), expected_total=Decimal("1280.00"))
        po_acme.lines = [
            PurchaseOrderLine(line_number=1, sku="SKU-DOCK", description="USB-C Dock", quantity=Decimal("10"), unit_price=Decimal("120.00"), tax_rate=Decimal("0.08")),
            PurchaseOrderLine(line_number=2, sku="SKU-CABLE", description="USB-C Cable", quantity=Decimal("10"), unit_price=Decimal("8.00"), tax_rate=Decimal("0.08")),
        ]
        po_harbor = PurchaseOrder(number="PO-2026-098", vendor_id=harbor.id, currency="USD", status="open", issue_date=date(2026, 9, 3), expected_total=Decimal("1000.00"))
        po_harbor.lines = [PurchaseOrderLine(line_number=1, sku="SKU-FILTER", description="Hydraulic Filter", quantity=Decimal("20"), unit_price=Decimal("50.00"), tax_rate=Decimal("0.08"))]
        po_northstar = PurchaseOrder(number="PO-2026-107", vendor_id=northstar.id, currency="USD", status="open", issue_date=date(2026, 9, 5), expected_total=Decimal("540.00"))
        po_northstar.lines = [PurchaseOrderLine(line_number=1, sku="SKU-PAPER", description="Recycled copy paper case", quantity=Decimal("30"), unit_price=Decimal("18.00"), tax_rate=Decimal("0.08"))]
        db.add_all([po_acme, po_harbor, po_northstar])
        db.flush()
        grn_acme = GoodsReceipt(number="GRN-2026-441", purchase_order_id=po_acme.id, received_date=date(2026, 9, 15), status="received")
        grn_acme.lines = [
            GoodsReceiptLine(purchase_order_line_id=po_acme.lines[0].id, sku="SKU-DOCK", description="USB-C Dock", quantity=Decimal("10")),
            GoodsReceiptLine(purchase_order_line_id=po_acme.lines[1].id, sku="SKU-CABLE", description="USB-C Cable", quantity=Decimal("10")),
        ]
        grn_harbor = GoodsReceipt(number="GRN-2026-398", purchase_order_id=po_harbor.id, received_date=date(2026, 9, 17), status="received")
        grn_harbor.lines = [GoodsReceiptLine(purchase_order_line_id=po_harbor.lines[0].id, sku="SKU-FILTER", description="Hydraulic Filter", quantity=Decimal("20"))]
        grn_north = GoodsReceipt(number="GRN-2026-426", purchase_order_id=po_northstar.id, received_date=date(2026, 9, 18), status="received")
        grn_north.lines = [GoodsReceiptLine(purchase_order_line_id=po_northstar.lines[0].id, sku="SKU-PAPER", description="Recycled copy paper case", quantity=Decimal("20"))]
        db.add_all([grn_acme, grn_harbor, grn_north])
        db.flush()

        # A reproducible, synthetic-only historical baseline. No fraud labels are inferred from these rows.
        rng = random.Random(20261012)
        vendors = [acme, harbor, northstar, nova]
        base_ranges = [(850, 4400), (900, 8200), (45, 850), (600, 2400)]
        base_day = date(2026, 1, 10)
        for index in range(36):
            vendor_index = index % len(vendors)
            vendor = vendors[vendor_index]
            low, high = base_ranges[vendor_index]
            amount = Decimal(str(round(rng.uniform(low, high), 2)))
            issue_date = base_day + timedelta(days=index * 6 + rng.randrange(0, 4))
            db.add(Invoice(
                invoice_number=f"HIST-{vendor.code}-{index + 1:04d}", vendor_id=vendor.id,
                currency="USD", issue_date=issue_date, subtotal=amount,
                tax_amount=(amount * Decimal("0.08")).quantize(Decimal("0.01")),
                total_amount=(amount * Decimal("1.08")).quantize(Decimal("0.01")),
                processing_status="completed", workflow_status="historical_reference",
                extraction_confidence=0.99, heuristic_risk_score=0.0,
                source="synthetic_history", is_synthetic=True,
            ))

        db.add_all([
            ModelVersion(component="document_extraction", version="rules-v1", status="active", metrics={"mode": "CPU rule-based baseline", "layoutlm_loaded": False}),
            ModelVersion(component="three_way_matching", version="match-rules-1.0", status="active", metrics={"tolerance_mode": settings.tolerance_policy}),
            ModelVersion(component="duplicate_detection", version="duplicate-v1", status="active", metrics={"exact_hash": True, "near_duplicate": "vendor+amount+date+invoice-number similarity"}),
            ModelVersion(component="benford_screen", version="benford-v1", status="active", metrics={"minimum_sample": settings.benford_min_sample, "standalone_fraud_proof": False}),
        ])
        db.add_all([
            ApplicationSetting(key="tolerance_absolute", value={"amount": settings.tolerance_absolute}),
            ApplicationSetting(key="tolerance_percent", value={"fraction": settings.tolerance_percent}),
            ApplicationSetting(key="tolerance_policy", value={"mode": settings.tolerance_policy}),
            ApplicationSetting(key="benford_min_sample", value={"count": settings.benford_min_sample}),
            ApplicationSetting(key="auto_payment_enabled", value={"enabled": False}),
            ApplicationSetting(key="risk_policy", value={"auto_payment": False, "initial_review_threshold": 0.25}),
        ])
        db.flush()

        fixture_definitions = [
            ("invoice_matched.pdf", acme, po_acme, "demo_valid"),
            ("invoice_near_duplicate.pdf", acme, po_acme, "demo_near_duplicate"),
            ("invoice_price_variance.pdf", harbor, po_harbor, "demo_price_variance"),
            ("invoice_missing_po.pdf", nova, None, "demo_missing_po"),
        ]
        job_ids: list[str] = []
        for fixture_name, vendor, po, source in fixture_definitions:
            source_path = docs_root / fixture_name
            if not source_path.exists():
                continue
            data = source_path.read_bytes()
            digest = sha256(data).hexdigest()
            invoice_id = new_id()
            invoice = Invoice(
                id=invoice_id, invoice_number=f"PENDING-{invoice_id[:8]}", vendor_id=vendor.id,
                purchase_order_id=po.id if po else None, currency="USD", processing_status="queued",
                workflow_status="processing", source=source, is_synthetic=True,
            )
            db.add(invoice)
            db.flush()
            stored_path = storage_dir / f"{invoice_id}-{fixture_name}"
            stored_path.write_bytes(data)
            db.add(DocumentFile(
                invoice_id=invoice_id, original_filename=fixture_name, stored_path=str(stored_path),
                mime_type="application/pdf", sha256=digest, byte_size=len(data),
            ))
            job = ProcessingJob(invoice_id=invoice_id, state="queued", stage="queued")
            db.add(job)
            db.flush()
            job_ids.append(job.id)
        append_audit_event(db, actor_id=None, action="demo.seeded", object_type="system", object_id="demo-dataset", details={"synthetic": True, "historical_invoice_count": 36, "sample_document_jobs": len(job_ids)})
        db.commit()

    # Process the sample documents through the same worker path used for uploads.
    if job_ids:
        from app.processing import run_processing_job
        for job_id in job_ids:
            run_processing_job(job_id, session_factory, storage_dir)
    return job_ids


def seed_current_database() -> None:
    engine = create_database_engine(settings.database_url)
    try:
        from app.db import Base
        Base.metadata.create_all(engine)
        session_factory = create_session_factory(engine)
        seed_demo_data(session_factory, settings.storage_path)
    finally:
        engine.dispose()


if __name__ == "__main__":
    seed_current_database()
