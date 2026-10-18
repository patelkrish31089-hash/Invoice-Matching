from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Column,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


user_role_assignments = Table(
    "user_role_assignments",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"), index=True, nullable=True)
    roles: Mapped[list["Role"]] = relationship(secondary=user_role_assignments, back_populates="users", lazy="selectin")


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    users: Mapped[list[User]] = relationship(secondary=user_role_assignments, back_populates="roles")


class AuthSession(Base):
    """Server-side revocable access-session metadata; never stores bearer tokens."""

    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ix_auth_sessions_user_expiry", "user_id", "expires_at_epoch"),
        Index("ix_auth_sessions_expiry_epoch", "expires_at_epoch"),
    )


class Vendor(Base, TimestampMixin):
    __tablename__ = "vendors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(48), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(180), index=True, nullable=False)
    tax_identifier: Mapped[str | None] = mapped_column(String(80), nullable=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    address_token: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    bank_token: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    risk_tier: Mapped[str] = mapped_column(String(24), default="standard", nullable=False)
    external_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    __table_args__ = (
        UniqueConstraint("external_source", "external_id", name="uq_vendors_external_identity"),
        CheckConstraint("(external_source IS NULL AND external_id IS NULL) OR (external_source IS NOT NULL AND external_id IS NOT NULL)", name="ck_vendors_external_identity_pair"),
    )


class Employee(Base, TimestampMixin):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    employee_code: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    address_token: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    bank_token: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PurchaseOrder(Base, TimestampMixin):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    number: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"), index=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"), nullable=False)
    external_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    vendor: Mapped[Vendor] = relationship()
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(back_populates="purchase_order", cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (
        UniqueConstraint("external_source", "external_id", name="uq_purchase_orders_external_identity"),
        CheckConstraint("(external_source IS NULL AND external_id IS NULL) OR (external_source IS NOT NULL AND external_id IS NOT NULL)", name="ck_purchase_orders_external_identity_pair"),
    )


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True, nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 5), default=Decimal("0"), nullable=False)
    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    __table_args__ = (UniqueConstraint("purchase_order_id", "line_number", name="uq_po_line_number"),)


class GoodsReceipt(Base, TimestampMixin):
    __tablename__ = "goods_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    number: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), index=True, nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="received", nullable=False)
    external_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    purchase_order: Mapped[PurchaseOrder] = relationship()
    lines: Mapped[list["GoodsReceiptLine"]] = relationship(back_populates="goods_receipt", cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (
        UniqueConstraint("external_source", "external_id", name="uq_goods_receipts_external_identity"),
        CheckConstraint("(external_source IS NULL AND external_id IS NULL) OR (external_source IS NOT NULL AND external_id IS NOT NULL)", name="ck_goods_receipts_external_identity_pair"),
    )


class GoodsReceiptLine(Base):
    __tablename__ = "goods_receipt_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goods_receipt_id: Mapped[str] = mapped_column(ForeignKey("goods_receipts.id", ondelete="CASCADE"), index=True, nullable=False)
    purchase_order_line_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_order_lines.id"), index=True, nullable=True)
    sku: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")


class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_number: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    vendor_id: Mapped[str | None] = mapped_column(ForeignKey("vendors.id"), index=True, nullable=True)
    purchase_order_id: Mapped[str | None] = mapped_column(ForeignKey("purchase_orders.id"), index=True, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    shipping_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"), nullable=False)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(32), default="uploaded", index=True, nullable=False)
    workflow_status: Mapped[str] = mapped_column(String(32), default="processing", index=True, nullable=False)
    extraction_confidence: Mapped[float | None] = mapped_column(nullable=True)
    heuristic_risk_score: Mapped[float | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="upload", nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    vendor: Mapped[Vendor | None] = relationship()
    purchase_order: Mapped[PurchaseOrder | None] = relationship()
    lines: Mapped[list["InvoiceLine"]] = relationship(back_populates="invoice", cascade="all, delete-orphan", lazy="selectin")
    documents: Mapped[list["DocumentFile"]] = relationship(back_populates="invoice", cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (Index("ix_invoice_vendor_number", "vendor_id", "invoice_number"),)


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(400), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 5), default=Decimal("0"), nullable=False)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    __table_args__ = (UniqueConstraint("invoice_id", "line_number", name="uq_invoice_line_number"),)


class DocumentFile(Base):
    __tablename__ = "document_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(600), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invoice: Mapped[Invoice] = relationship(back_populates="documents")


class ExtractionResult(Base):
    __tablename__ = "extraction_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    extracted_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    tokens: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    engine_name: Mapped[str] = mapped_column(String(80), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(100), nullable=False)
    extraction_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InvoiceMatchResult(Base):
    __tablename__ = "invoice_match_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), default="match-rules-1.0", nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FraudAssessment(Base):
    __tablename__ = "fraud_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    heuristic_score: Mapped[float] = mapped_column(nullable=False)
    risk_band: Mapped[str] = mapped_column(String(24), nullable=False)
    is_probability: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    model_versions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FraudAlert(Base):
    __tablename__ = "fraud_alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    assessment_id: Mapped[str | None] = mapped_column(ForeignKey("fraud_assessments.id", ondelete="SET NULL"), nullable=True)
    alert_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    severity: Mapped[float] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewCase(Base, TimestampMixin):
    __tablename__ = "review_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True, nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    assigned_to_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    requested_documents: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice: Mapped[Invoice] = relationship()
    assigned_to: Mapped[User | None] = relationship()


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("review_cases.id", ondelete="CASCADE"), index=True, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PaymentProposal(Base, TimestampMixin):
    """Human-approved, non-executable payment metadata; contains no bank instructions."""

    __tablename__ = "payment_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"), index=True, nullable=False)
    review_decision_id: Mapped[str | None] = mapped_column(ForeignKey("review_decisions.id", ondelete="RESTRICT"), nullable=True)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    vendor_code: Mapped[str] = mapped_column(String(48), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(180), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending_approval", nullable=False)
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handoff_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_handoff_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (
        UniqueConstraint("invoice_id", "version", name="uq_payment_proposal_invoice_version"),
        CheckConstraint("amount > 0", name="ck_payment_proposal_amount_positive"),
        CheckConstraint("version > 0", name="ck_payment_proposal_version_positive"),
        CheckConstraint("handoff_count >= 0", name="ck_payment_proposal_handoff_nonnegative"),
        CheckConstraint("length(currency) = 3", name="ck_payment_proposal_currency_length"),
        CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'cancelled')",
            name="ck_payment_proposal_status",
        ),
        Index("ix_payment_proposals_status_created_at", "status", "created_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sequence: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    object_type: Mapped[str] = mapped_column(String(60), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), default="0" * 64, nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(60), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessingOutbox(Base):
    """Durable dispatch intent written in the same transaction as a processing job."""

    __tablename__ = "processing_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(60), default="process_invoice", nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("job_id", "event_type", name="uq_processing_outbox_job_event"),
        Index("ix_processing_outbox_due", "state", "available_at"),
    )


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    component: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="candidate", nullable=False)
    artifact_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    data_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("component", "version", name="uq_model_component_version"),)


class VendorRiskProfile(Base):
    __tablename__ = "vendor_risk_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), unique=True, nullable=False)
    invoice_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rolling_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    risk_indicators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ApplicationSetting(Base):
    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ModelFeedback(Base):
    __tablename__ = "model_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    correction_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_for_training: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UploadReceipt(Base):
    """Durable upload intent used to recover from crashes around file/database commits."""
    __tablename__ = "upload_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(24), default="upload", nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    staging_path: Mapped[str] = mapped_column(String(600), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(600), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="receiving", index=True, nullable=False)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("actor_id", "idempotency_key_hash", name="uq_upload_receipt_actor_key_hash"),
        UniqueConstraint("source", "source_reference", name="uq_upload_receipt_source_ref"),
        Index("ix_upload_receipt_state_updated", "state", "updated_at"),
    )


class EmailIngestionEvent(Base):
    """Idempotency and receipt metadata for signed RFC822 webhook events; no raw email is stored."""
    __tablename__ = "email_ingestion_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_event_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuarantinedUpload(Base):
    """Metadata for infected documents stored outside the normal invoice storage root."""
    __tablename__ = "quarantined_uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    quarantine_path: Mapped[str] = mapped_column(String(600), nullable=False)
    scanner_name: Mapped[str] = mapped_column(String(80), nullable=False)
    scanner_version: Mapped[str | None] = mapped_column(String(160), nullable=True)
    detection: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="quarantined", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("source", "source_reference", name="uq_quarantined_upload_source_ref"),
        Index("ix_quarantined_upload_status_created", "status", "created_at"),
    )
