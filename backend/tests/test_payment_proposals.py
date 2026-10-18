from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AuditEvent,
    FraudAlert,
    Invoice,
    InvoiceMatchResult,
    PaymentProposal,
    ReviewCase,
    ReviewDecision,
    Role,
    User,
    Vendor,
)
from app.security import hash_password


def _user_headers(api, email: str, role_names: list[str], display_name: str | None = None) -> dict[str, str]:
    db = api["app"].state.session_factory()
    roles = list(db.scalars(select(Role).where(Role.name.in_(role_names))).all())
    assert {role.name for role in roles} == set(role_names)
    password = "ProposalTestPass123!"
    user = User(
        email=email,
        display_name=display_name or email.split("@", 1)[0],
        password_hash=hash_password(password),
        roles=roles,
    )
    db.add(user)
    db.commit()
    db.close()
    response = api["client"].post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _invoice(api, *, outcome: str = "MATCHED", workflow: str = "ready_for_approval", review_user_id: str | None = None, open_case: bool = False, open_alert: bool = False) -> str:
    db = api["app"].state.session_factory()
    suffix = uuid4().hex[:10].upper()
    vendor = Vendor(code=f"PV-{suffix}", name=f"Proposal Vendor {suffix}", risk_tier="standard")
    invoice = Invoice(
        invoice_number=f"INV-PROP-{suffix}",
        vendor=vendor,
        currency="USD",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("8.00"),
        total_amount=Decimal("108.00"),
        processing_status="completed",
        workflow_status=workflow,
        heuristic_risk_score=0.02,
        source="synthetic_test",
        is_synthetic=True,
    )
    db.add(invoice)
    db.flush()
    db.add(InvoiceMatchResult(
        invoice_id=invoice.id,
        outcome=outcome,
        details={"outcome": outcome, "test_fixture": True},
    ))
    if review_user_id or open_case:
        review_case = ReviewCase(
            invoice_id=invoice.id,
            status="approved" if review_user_id else "open",
            priority="normal",
            reason_codes=["synthetic_test"],
            summary="Synthetic proposal acceptance test.",
        )
        db.add(review_case)
        db.flush()
        if review_user_id:
            db.add(ReviewDecision(
                case_id=review_case.id,
                reviewer_id=review_user_id,
                decision="approved",
                reason="Reviewed source document and approved the test exception.",
                evidence={"synthetic": True},
            ))
    if open_alert:
        db.add(FraudAlert(
            invoice_id=invoice.id,
            alert_type="test_signal",
            severity=0.7,
            title="Synthetic open alert",
            explanation="This test alert must block proposal creation.",
            evidence={"synthetic": True},
            status="open",
        ))
    db.commit()
    invoice_id = invoice.id
    db.close()
    return invoice_id


def _create_proposal(client: TestClient, headers: dict[str, str], invoice_id: str):
    return client.post(
        "/api/v1/payment-proposals",
        headers=headers,
        json={"invoice_id": invoice_id, "reason": "Reviewed invoice packet for a payment proposal."},
    )


def _user_id(api, email: str) -> str:
    db = api["app"].state.session_factory()
    user = db.scalar(select(User).where(User.email == email))
    assert user is not None
    user_id = user.id
    db.close()
    return user_id


def test_proposal_requires_eligibility_independent_approver_and_never_executes(api):
    client = api["client"]
    invoice_id = _invoice(api)
    create = _create_proposal(client, api["admin"], invoice_id)
    assert create.status_code == 201, create.text
    body = create.json()
    proposal_id = body["id"]
    assert body["status"] == "pending_approval"
    assert body["payment_executed"] is False
    assert body["executable"] is False
    assert body["payment_rail_configured"] is False

    # A general admin, even one who can create a proposal, is not implicitly an approver.
    assert client.post(
        f"/api/v1/payment-proposals/{proposal_id}/decision",
        headers=api["admin"],
        json={"decision": "approve", "reason": "Independent approval review complete."},
    ).status_code == 403

    approver = _user_headers(api, "separate.approver@test.local", ["approver"])
    decided = client.post(
        f"/api/v1/payment-proposals/{proposal_id}/decision",
        headers=approver,
        json={"decision": "approve", "reason": "Independent approval review complete."},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["proposal"]["status"] == "approved"
    assert decided.json()["payment_executed"] is False
    assert decided.json()["executable"] is False
    assert decided.json()["next_step"].startswith("A separate human-controlled process")

    # Approval changes only the proposal record; it does not advance invoice/payment state.
    db = api["app"].state.session_factory()
    invoice = db.get(Invoice, invoice_id)
    proposal = db.get(PaymentProposal, proposal_id)
    assert invoice.workflow_status == "ready_for_approval"
    assert proposal.status == "approved"
    assert not hasattr(proposal, "payment_id")
    assert db.scalar(select(AuditEvent.id).where(
        AuditEvent.object_type == "payment_proposal",
        AuditEvent.object_id == proposal_id,
        AuditEvent.action == "payment_proposal.decided",
    ))
    db.close()

    assert client.post(
        f"/api/v1/payment-proposals/{proposal_id}/decision",
        headers=approver,
        json={"decision": "approve", "reason": "Duplicate approval must not be accepted."},
    ).status_code == 409


def test_match_exceptions_need_review_and_unresolved_alerts_or_cases_block(api):
    client = api["client"]
    unmatched = _invoice(api, outcome="AMOUNT_MISMATCH")
    blocked = _create_proposal(client, api["admin"], unmatched)
    assert blocked.status_code == 409
    assert "matching exception" in blocked.json()["detail"]

    open_case_invoice = _invoice(api, open_case=True)
    open_case_result = _create_proposal(client, api["admin"], open_case_invoice)
    assert open_case_result.status_code == 409
    assert "workflow state" in open_case_result.json()["detail"] or "unresolved review" in open_case_result.json()["detail"]

    open_alert_invoice = _invoice(api, open_alert=True)
    open_alert_result = _create_proposal(client, api["admin"], open_alert_invoice)
    assert open_alert_result.status_code == 409
    assert "fraud alert" in open_alert_result.json()["detail"]

    # A durable reviewer decision can authorize proposal consideration for a matching exception.
    reviewer_id = _user_id(api, "reviewer@test.local")
    reviewed_invoice = _invoice(
        api, outcome="AMOUNT_MISMATCH", workflow="approved_by_reviewer", review_user_id=reviewer_id,
    )
    accepted = _create_proposal(client, api["admin"], reviewed_invoice)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["review_decision_id"]


def test_same_person_cannot_create_and_approve_or_reuse_a_review_decision(api):
    client = api["client"]
    same_person = _user_headers(api, "proposer.approver@test.local", ["admin", "approver"])
    invoice_id = _invoice(api)
    proposal = _create_proposal(client, same_person, invoice_id)
    assert proposal.status_code == 201, proposal.text
    rejected = client.post(
        f"/api/v1/payment-proposals/{proposal.json()['id']}/decision",
        headers=same_person,
        json={"decision": "approve", "reason": "This is not an independent approver."},
    )
    assert rejected.status_code == 403
    assert "separate person" in rejected.json()["detail"]

    # Even a distinct proposal author cannot have previously submitted, corrected, or reviewed the invoice.
    review_actor = _user_headers(api, "reviewer.approver@test.local", ["reviewer", "approver"])
    reviewed_invoice = _invoice(
        api, outcome="AMOUNT_MISMATCH", workflow="approved_by_reviewer",
        review_user_id=_user_id(api, "reviewer.approver@test.local"),
    )
    reviewed_proposal = _create_proposal(client, api["admin"], reviewed_invoice)
    assert reviewed_proposal.status_code == 201, reviewed_proposal.text
    blocked = client.post(
        f"/api/v1/payment-proposals/{reviewed_proposal.json()['id']}/decision",
        headers=review_actor,
        json={"decision": "approve", "reason": "Prior reviewer cannot approve payment proposal."},
    )
    assert blocked.status_code == 403


def test_changed_invoice_state_blocks_approval_until_cancel_and_new_version(api):
    client = api["client"]
    invoice_id = _invoice(api)
    first = _create_proposal(client, api["admin"], invoice_id)
    assert first.status_code == 201, first.text
    proposal_id = first.json()["id"]
    approver = _user_headers(api, "stale.approver@test.local", ["approver"])

    db = api["app"].state.session_factory()
    invoice = db.get(Invoice, invoice_id)
    invoice.total_amount = Decimal("999.00")
    db.commit()
    db.close()
    stale = client.post(
        f"/api/v1/payment-proposals/{proposal_id}/decision",
        headers=approver,
        json={"decision": "approve", "reason": "Attempt against changed source data."},
    )
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]

    cancelled = client.post(
        f"/api/v1/payment-proposals/{proposal_id}/cancel",
        headers=api["admin"],
        json={"reason": "Invoice total changed; replacing the stale proposal."},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["proposal"]["status"] == "cancelled"
    second = _create_proposal(client, api["admin"], invoice_id)
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2
    assert second.json()["amount"] == "999.00"


def test_handoff_requires_third_actor_and_is_local_non_executable_packet(api):
    client = api["client"]
    invoice_id = _invoice(api)
    created = _create_proposal(client, api["admin"], invoice_id)
    assert created.status_code == 201, created.text
    proposal_id = created.json()["id"]
    approver = _user_headers(api, "handoff.approver@test.local", ["approver"])
    decision = client.post(
        f"/api/v1/payment-proposals/{proposal_id}/decision",
        headers=approver,
        json={"decision": "approve", "reason": "Approved for a non-executable review packet."},
    )
    assert decision.status_code == 200

    # Neither the proposal author nor the approver can perform the third-person handoff.
    creator_handoff = client.post(
        "/api/v1/payment-proposals/handoff",
        headers=api["admin"],
        json={"proposal_ids": [proposal_id], "reason": "Creator cannot hand off their proposal."},
    )
    assert creator_handoff.status_code == 403
    approver_handoff = client.post(
        "/api/v1/payment-proposals/handoff",
        headers=approver,
        json={"proposal_ids": [proposal_id], "reason": "Approver cannot perform the handoff."},
    )
    assert approver_handoff.status_code == 403

    third_actor = _user_headers(api, "handoff.clerk@test.local", ["ap_clerk"])
    response = client.post(
        "/api/v1/payment-proposals/handoff",
        headers=third_actor,
        json={"proposal_ids": [proposal_id], "reason": "Preparing a separate internal review packet."},
    )
    assert response.status_code == 200, response.text
    assert "attachment;" in response.headers.get("content-disposition", "")
    packet = response.json()
    assert packet["not_payment_instruction"] is True
    assert packet["payment_execution_enabled"] is False
    assert packet["payment_executed"] is False
    assert packet["executable"] is False
    assert packet["external_network_call"] is False
    assert packet["items"][0]["amount"] == "108.00"
    assert "bank_token" not in str(packet).lower()
    assert "account_number" not in str(packet).lower()

    db = api["app"].state.session_factory()
    proposal = db.get(PaymentProposal, proposal_id)
    assert proposal.handoff_count == 1
    assert proposal.last_handoff_by_id is not None
    assert db.scalar(select(AuditEvent.id).where(
        AuditEvent.object_type == "payment_proposal",
        AuditEvent.object_id == proposal_id,
        AuditEvent.action == "payment_proposal.handoff_exported",
    ))
    db.close()
    cannot_recall = client.post(
        f"/api/v1/payment-proposals/{proposal_id}/cancel",
        headers=api["admin"],
        json={"reason": "Cannot cancel after the packet was generated."},
    )
    assert cannot_recall.status_code == 409


def test_read_only_roles_cannot_create_and_proposal_list_is_auditable(api):
    client = api["client"]
    invoice_id = _invoice(api)
    assert _create_proposal(client, api["auditor"], invoice_id).status_code == 403
    created = _create_proposal(client, api["admin"], invoice_id)
    assert created.status_code == 201
    result = client.get(
        f"/api/v1/payment-proposals?invoice_id={invoice_id}",
        headers=api["admin"],
    )
    assert result.status_code == 200
    assert result.json()["total"] == 1
    assert result.json()["payment_execution_enabled"] is False
    assert result.json()["items"][0]["id"] == created.json()["id"]


def test_administrator_can_provision_the_dedicated_approver_role(api):
    response = api["client"].post(
        "/api/v1/admin/users",
        headers=api["admin"],
        json={
            "email": "created.approver@test.local",
            "display_name": "Proposal Approver",
            "password": "NewApproverPass123!",
            "roles": ["approver"],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["roles"] == ["approver"]


def test_approver_can_inspect_invoice_and_proposal_but_cannot_edit_or_review(api):
    client = api["client"]
    invoice_id = _invoice(api)
    approver = _user_headers(
        api,
        "read.scope.approver@test.local",
        ["approver"],
    )

    invoice_list = client.get("/api/v1/invoices?limit=100", headers=approver)
    assert invoice_list.status_code == 200
    assert any(item["id"] == invoice_id for item in invoice_list.json()["items"])

    invoice_detail = client.get(f"/api/v1/invoices/{invoice_id}", headers=approver)
    assert invoice_detail.status_code == 200
    assert invoice_detail.json()["id"] == invoice_id

    proposal_list = client.get(
        f"/api/v1/payment-proposals?invoice_id={invoice_id}",
        headers=approver,
    )
    assert proposal_list.status_code == 200
    assert proposal_list.json()["items"] == []

    correction = client.patch(
        f"/api/v1/invoices/{invoice_id}/extraction",
        headers=approver,
        json={"values": {"invoice_number": "MUST-NOT-WRITE"}, "reason": "Approver should not alter source extraction."},
    )
    assert correction.status_code == 403

    review_decision = client.post(
        "/api/v1/review/cases/not-a-real-case/decision",
        headers=approver,
        json={"decision": "approved", "reason": "Approver is not a reviewer.", "evidence": {}},
    )
    assert review_decision.status_code == 403
