from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=12, max_length=256)
    roles: list[Literal["admin", "reviewer", "ap_clerk", "approver", "auditor", "vendor"]] = Field(min_length=1)
    vendor_id: str | None = Field(default=None, max_length=36)


class UserRoleUpdateRequest(BaseModel):
    roles: list[Literal["admin", "reviewer", "ap_clerk", "approver", "auditor", "vendor"]] = Field(min_length=1)
    vendor_id: str | None = Field(default=None, max_length=36)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class UserSummary(BaseModel):
    id: str
    email: str
    display_name: str
    roles: list[str]
    vendor_id: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserSummary


class InvoiceCorrectionRequest(BaseModel):
    values: dict[str, Any]
    reason: str = Field(min_length=5, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class ReviewAssignmentRequest(BaseModel):
    assigned_to_id: str


class ReviewDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected", "escalated", "request_documents", "confirmed_suspicious", "false_positive"]
    reason: str = Field(min_length=5, max_length=2000)
    evidence: dict[str, Any] = Field(default_factory=dict)


class VendorCreateRequest(BaseModel):
    code: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=2, max_length=180)
    tax_identifier: str | None = Field(default=None, max_length=80)
    aliases: list[str] = Field(default_factory=list)
    risk_tier: Literal["standard", "elevated", "restricted"] = "standard"


class PurchaseOrderLineInput(BaseModel):
    line_number: int = Field(ge=1)
    sku: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)


class PurchaseOrderCreateRequest(BaseModel):
    number: str = Field(min_length=2, max_length=80)
    vendor_id: str
    currency: str = Field(default="USD", min_length=3, max_length=3)
    status: Literal["open", "closed", "cancelled"] = "open"
    issue_date: date
    lines: list[PurchaseOrderLineInput] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str) -> str:
        return value.upper()


class ReceiptLineInput(BaseModel):
    purchase_order_line_id: str | None = None
    sku: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    quantity: Decimal = Field(gt=0)


class GoodsReceiptCreateRequest(BaseModel):
    number: str = Field(min_length=2, max_length=80)
    purchase_order_id: str
    received_date: date
    lines: list[ReceiptLineInput] = Field(min_length=1)


class SettingsUpdateRequest(BaseModel):
    tolerance_absolute: Decimal | None = Field(default=None, ge=0)
    tolerance_percent: Decimal | None = Field(default=None, ge=0, le=1)
    tolerance_policy: Literal["stricter", "either"] | None = None
    benford_min_sample: int | None = Field(default=None, ge=25, le=10000)


class StaleProcessingJobRecoveryRequest(BaseModel):
    expected_state: Literal["queued", "processing"]
    stale_after_minutes: int = Field(ge=5, le=1440)
    reason: str = Field(min_length=8, max_length=500)
    model_config = ConfigDict(extra="forbid")


class OutboxRetryRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=500)
    model_config = ConfigDict(extra="forbid")


class JobRetryResponse(BaseModel):
    invoice_id: str
    job_id: str
    state: str
    message: str


class UploadOperationStatusResponse(BaseModel):
    status: Literal["not_found", "in_progress", "abandoned", "committed", "recovery_required"]
    receipt_state: str | None = None
    original_filename: str | None = None
    receipt_updated_at: datetime | None = None
    invoice_id: str | None = None
    invoice_number: str | None = None
    processing_status: str | None = None
    workflow_status: str | None = None
    job_id: str | None = None
    job_state: str | None = None
    job_stage: str | None = None
    dispatch_state: str | None = None
    dispatch_attempts: int | None = None


class PaymentProposalCreateRequest(BaseModel):
    invoice_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=10, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class PaymentProposalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=10, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class PaymentProposalCancelRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=1000)
    model_config = ConfigDict(extra="forbid")


class PaymentProposalHandoffRequest(BaseModel):
    proposal_ids: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=10, max_length=1000)
    model_config = ConfigDict(extra="forbid")
