from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable
import re

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    class _Fuzz:
        @staticmethod
        def token_set_ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100
    fuzz = _Fuzz()


CENT = Decimal("0.01")


def D(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def normalize_description(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


@dataclass(frozen=True)
class TolerancePolicy:
    absolute: Decimal = Decimal("0.05")
    percent: Decimal = Decimal("0.005")
    mode: str = "stricter"

    def allowed(self, expected: Decimal) -> Decimal:
        relative = abs(expected) * self.percent
        if expected == 0:
            relative = self.absolute
        if self.mode == "either":
            return max(self.absolute, relative)
        return min(self.absolute, relative)

    def accepts(self, expected: Decimal, actual: Decimal) -> bool:
        return abs(actual - expected) <= self.allowed(expected)


def _best_line_match(invoice_line: dict[str, Any], po_lines: list[dict[str, Any]], unused: set[str]) -> tuple[dict[str, Any] | None, float, str]:
    sku = (invoice_line.get("sku") or "").strip().lower()
    if sku:
        for line in po_lines:
            if line["id"] in unused and line.get("sku") and line["sku"].strip().lower() == sku:
                return line, 100.0, "exact_sku"
    description = normalize_description(invoice_line.get("description"))
    candidates: list[tuple[float, dict[str, Any]]] = []
    for line in po_lines:
        if line["id"] not in unused:
            continue
        if sku and line.get("sku") and sku in line["sku"].lower():
            candidates.append((98.0, line))
            continue
        score = float(fuzz.token_set_ratio(description, normalize_description(line.get("description", ""))))
        candidates.append((score, line))
    if not candidates:
        return None, 0.0, "no_candidate"
    score, candidate = max(candidates, key=lambda item: item[0])
    return candidate, score, "fuzzy_description"


def _receipt_quantities(
    po_lines: list[dict[str, Any]], receipts: list[dict[str, Any]],
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    """Allocate each GRN line to at most one PO line; never reuse one receipt across duplicate SKUs."""
    totals = {str(line["id"]): Decimal("0") for line in po_lines}
    ambiguous: list[dict[str, Any]] = []
    by_id = {str(line["id"]): line for line in po_lines}

    for receipt in receipts:
        for receipt_line in receipt.get("lines", []):
            explicit_id = receipt_line.get("purchase_order_line_id")
            if explicit_id:
                matched = by_id.get(str(explicit_id))
                if matched is None:
                    ambiguous.append({"receipt": receipt.get("number"), "description": receipt_line.get("description"), "reason": "Receipt points to a line outside this PO."})
                    continue
                totals[str(explicit_id)] += D(receipt_line.get("quantity"))
                continue

            sku = (receipt_line.get("sku") or "").strip().casefold()
            sku_candidates = [line for line in po_lines if sku and (line.get("sku") or "").strip().casefold() == sku]
            candidates = sku_candidates if sku_candidates else po_lines
            description = normalize_description(receipt_line.get("description"))
            ranked = sorted(
                ((float(fuzz.token_set_ratio(description, normalize_description(line.get("description", "")))), line) for line in candidates),
                key=lambda item: item[0], reverse=True,
            )
            if sku_candidates and len(sku_candidates) == 1:
                chosen = sku_candidates[0]
            elif ranked and ranked[0][0] >= 85 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 5):
                chosen = ranked[0][1]
            else:
                ambiguous.append({"receipt": receipt.get("number"), "description": receipt_line.get("description"), "reason": "Receipt line lacks a PO-line link and could not be matched uniquely."})
                continue
            totals[str(chosen["id"])] += D(receipt_line.get("quantity"))
    return totals, ambiguous


def reconcile(
    invoice: dict[str, Any],
    purchase_order: dict[str, Any] | None,
    goods_receipts: list[dict[str, Any]] | None,
    *,
    policy: TolerancePolicy | None = None,
    previous_invoice_quantities: dict[int | str, Decimal | str] | None = None,
) -> dict[str, Any]:
    policy = policy or TolerancePolicy()
    receipts = goods_receipts or []
    prior_quantities = {str(key): D(value) for key, value in (previous_invoice_quantities or {}).items()}
    discrepancies: list[dict[str, Any]] = []
    line_results: list[dict[str, Any]] = []
    signals: list[str] = []
    financial_variance = False
    partial_delivery = False
    needs_manual = False

    if purchase_order is None:
        return {
            "outcome": "MISSING_PO",
            "explanation": "No purchase order was linked to this invoice. This is an exception, not a fraud conclusion.",
            "line_results": [], "discrepancies": [{"type": "missing_po", "expected": None, "actual": invoice.get("purchase_order_number"), "difference": None, "rule": "PO reference must resolve"}],
            "financial_exposure": str(D(invoice.get("total_amount"))), "partial_receipt": False,
        }
    po_status = str(purchase_order.get("status") or "open").casefold()
    po_status_problem = po_status in {"closed", "cancelled", "canceled"}
    if po_status_problem:
        issue_type = "po_cancelled" if po_status in {"cancelled", "canceled"} else "po_closed"
        discrepancies.append({
            "type": issue_type, "expected": "open PO", "actual": po_status,
            "difference": None, "rule": "Invoices against closed or cancelled POs require an explicit human exception review.",
        })
        signals.append(issue_type)
    if not receipts:
        missing_grn = {"type": "missing_grn", "expected": purchase_order.get("number"), "actual": None, "difference": None, "rule": "At least one goods receipt is required"}
        discrepancies.append(missing_grn)
        return {
            "outcome": "MISMATCH" if po_status_problem else "MISSING_GRN",
            "explanation": "The PO is closed/cancelled and no goods receipt is linked; resolve both exceptions before matching." if po_status_problem else "No goods receipt was linked to this purchase order; three-way matching cannot complete.",
            "line_results": [], "discrepancies": discrepancies,
            "financial_exposure": str(D(invoice.get("total_amount"))), "partial_receipt": False,
        }

    invoice_vendor = invoice.get("vendor_id")
    po_vendor = purchase_order.get("vendor_id")
    if invoice_vendor and po_vendor and invoice_vendor != po_vendor:
        discrepancies.append({"type": "vendor_mismatch", "expected": po_vendor, "actual": invoice_vendor, "difference": None, "rule": "Invoice and PO vendor identifiers must match"})
        signals.append("vendor_mismatch")
    elif not invoice_vendor or not po_vendor:
        discrepancies.append({"type": "vendor_identity_unresolved", "expected": po_vendor, "actual": invoice_vendor or invoice.get("vendor_name"), "difference": None, "rule": "Both invoice and PO must resolve to a verified vendor before an automatic match"})
        needs_manual = True
    invoice_currency = (invoice.get("currency") or "USD").upper()
    po_currency = (purchase_order.get("currency") or "USD").upper()
    if invoice_currency != po_currency:
        discrepancies.append({"type": "currency_mismatch", "expected": po_currency, "actual": invoice_currency, "difference": None, "rule": "No automatic FX conversion is applied"})
        signals.append("currency_mismatch")
    if D(invoice.get("shipping_amount")) != 0:
        discrepancies.append({"type": "freight_unallocated", "expected": "a PO freight/surcharge allowance", "actual": str(D(invoice.get("shipping_amount"))), "difference": None, "rule": "The PO schema has no approved freight allowance; verify shipping charges manually."})
        needs_manual = True
    if D(invoice.get("discount_amount")) != 0:
        discrepancies.append({"type": "discount_unallocated", "expected": "a PO discount term", "actual": str(D(invoice.get("discount_amount"))), "difference": None, "rule": "The PO schema has no negotiated discount terms; verify the discount manually."})
        needs_manual = True

    po_lines = purchase_order.get("lines", [])
    unused = {line["id"] for line in po_lines}
    received_by_po, ambiguous_receipts = _receipt_quantities(po_lines, receipts)
    if ambiguous_receipts:
        needs_manual = True
        for item in ambiguous_receipts:
            discrepancies.append({"type": "ambiguous_receipt_line", "expected": "one uniquely linked PO line", "actual": item.get("description"), "difference": None, "rule": item.get("reason"), "receipt": item.get("receipt")})
    invoice_quantities_by_po: dict[str, Decimal] = {}
    extended_expected = Decimal("0")
    expected_tax = Decimal("0")
    received_any = False
    for invoice_line in invoice.get("lines", []):
        po_line, score, method = _best_line_match(invoice_line, po_lines, unused)
        if po_line is None or score < 70:
            line_results.append({"invoice_line": invoice_line.get("description"), "po_line": None, "status": "unmatched", "similarity": round(score, 1), "explanation": "No sufficiently similar open PO line was found."})
            discrepancies.append({"type": "unmatched_invoice_line", "expected": None, "actual": invoice_line.get("description"), "difference": None, "rule": "Line similarity must be at least 70"})
            needs_manual = True
            continue
        if score < 85 and method != "exact_sku":
            needs_manual = True
        unused.discard(po_line["id"])
        inv_qty = D(invoice_line.get("quantity"))
        po_qty = D(po_line.get("quantity"))
        inv_price = D(invoice_line.get("unit_price"))
        po_price = D(po_line.get("unit_price"))
        received_qty = received_by_po.get(str(po_line["id"]), Decimal("0"))
        received_any = received_any or received_qty > 0
        po_line_key = str(po_line["line_number"])
        previously_invoiced = prior_quantities.get(po_line_key, Decimal("0"))
        current_invoice_before_line = invoice_quantities_by_po.get(po_line["id"], Decimal("0"))
        cumulative_invoiced = previously_invoiced + current_invoice_before_line + inv_qty
        invoice_quantities_by_po[po_line["id"]] = current_invoice_before_line + inv_qty
        line_total = D(invoice_line.get("line_total"), default=str(inv_qty * inv_price))
        calculated_line_total = money(inv_qty * inv_price)
        line_status = "matched"
        if inv_qty > po_qty:
            discrepancies.append({"type": "quantity_above_ordered", "line": po_line["line_number"], "expected": str(po_qty), "actual": str(inv_qty), "difference": str(inv_qty - po_qty), "rule": "Invoiced quantity cannot exceed PO quantity"})
            line_status = "mismatch"
        if inv_qty > received_qty:
            discrepancies.append({"type": "quantity_above_received", "line": po_line["line_number"], "expected": str(received_qty), "actual": str(inv_qty), "difference": str(inv_qty - received_qty), "rule": "Invoiced quantity cannot exceed received quantity"})
            line_status = "mismatch"
        if previously_invoiced > 0 and inv_qty <= po_qty and cumulative_invoiced > po_qty:
            discrepancies.append({
                "type": "cumulative_quantity_above_ordered", "line": po_line["line_number"],
                "previously_invoiced": str(previously_invoiced + current_invoice_before_line),
                "expected": str(po_qty), "actual": str(cumulative_invoiced),
                "difference": str(cumulative_invoiced - po_qty),
                "rule": "Cumulative non-rejected invoices against a PO line cannot exceed the ordered quantity.",
            })
            line_status = "mismatch"
        if previously_invoiced > 0 and inv_qty <= received_qty and cumulative_invoiced > received_qty:
            discrepancies.append({
                "type": "cumulative_quantity_above_received", "line": po_line["line_number"],
                "previously_invoiced": str(previously_invoiced + current_invoice_before_line),
                "expected": str(received_qty), "actual": str(cumulative_invoiced),
                "difference": str(cumulative_invoiced - received_qty),
                "rule": "Cumulative non-rejected invoices against a PO line cannot exceed received quantity.",
            })
            line_status = "mismatch"
        price_delta = inv_price - po_price
        if not policy.accepts(po_price, inv_price):
            discrepancies.append({"type": "unit_price_variance", "line": po_line["line_number"], "expected": str(po_price), "actual": str(inv_price), "difference": str(price_delta), "rule": f"Allowed absolute difference {policy.allowed(po_price)} under {policy.mode} tolerance"})
            line_status = "mismatch"
        elif price_delta != 0:
            discrepancies.append({"type": "unit_price_within_tolerance", "line": po_line["line_number"], "expected": str(po_price), "actual": str(inv_price), "difference": str(price_delta), "rule": f"Accepted within {policy.allowed(po_price)} tolerance under {policy.mode} policy"})
            line_status = "within_tolerance"
        line_delta = line_total - calculated_line_total
        if not policy.accepts(calculated_line_total, line_total):
            discrepancies.append({"type": "line_arithmetic", "line": po_line["line_number"], "expected": str(calculated_line_total), "actual": str(line_total), "difference": str(line_delta), "rule": "Line total must equal quantity × unit price within tolerance"})
            line_status = "mismatch"
        elif line_delta != 0:
            discrepancies.append({"type": "line_arithmetic_within_tolerance", "line": po_line["line_number"], "expected": str(calculated_line_total), "actual": str(line_total), "difference": str(line_delta), "rule": "Line arithmetic variance accepted within configured tolerance"})
            if line_status == "matched":
                line_status = "within_tolerance"
        expected_for_received = min(received_qty, po_qty) * po_price
        extended_expected += expected_for_received
        # Tax expectation is for this invoice's quantity, not every unit received against the PO.
        expected_for_invoice = inv_qty * po_price
        expected_tax += expected_for_invoice * D(po_line.get("tax_rate"))
        line_results.append({
            "invoice_line": invoice_line.get("description"), "po_line": po_line.get("description"),
            "po_line_number": po_line.get("line_number"), "similarity": round(score, 1), "method": method,
            "invoice_quantity": str(inv_qty), "previously_invoiced_quantity": str(previously_invoiced + current_invoice_before_line),
            "cumulative_invoiced_quantity": str(cumulative_invoiced),
            "ordered_quantity": str(po_qty), "received_quantity": str(received_qty),
            "invoice_unit_price": str(inv_price), "po_unit_price": str(po_price),
            "price_difference": str(price_delta), "line_total": str(line_total), "status": line_status,
        })

    for po_line in po_lines:
        received_qty = received_by_po.get(str(po_line["id"]), Decimal("0"))
        if received_qty < D(po_line.get("quantity")):
            partial_delivery = True
        invoiced = invoice_quantities_by_po.get(po_line["id"], Decimal("0"))
        if invoiced > received_qty:
            # Already reported at invoice line level; preserve a clear line mapping in the summary.
            pass
    if not received_any:
        discrepancies.append({"type": "no_receipt_lines", "expected": "PO line receipt quantities", "actual": 0, "difference": None, "rule": "A GRN must contain a line linked to the PO"})

    invoice_lines_sum = sum((D(line.get("line_total"), default=str(D(line.get("quantity")) * D(line.get("unit_price")))) for line in invoice.get("lines", [])), Decimal("0"))
    subtotal = invoice.get("subtotal")
    if subtotal is not None and not policy.accepts(invoice_lines_sum, D(subtotal)):
        financial_variance = True
        discrepancies.append({"type": "subtotal_variance", "expected": str(money(invoice_lines_sum)), "actual": str(D(subtotal)), "difference": str(D(subtotal) - invoice_lines_sum), "rule": f"Subtotal must reconcile to extracted line totals within {policy.allowed(invoice_lines_sum)}"})
    elif subtotal is not None and D(subtotal) != invoice_lines_sum:
        discrepancies.append({"type": "subtotal_within_tolerance", "expected": str(money(invoice_lines_sum)), "actual": str(D(subtotal)), "difference": str(D(subtotal) - invoice_lines_sum), "rule": "Subtotal rounding variance accepted within configured tolerance"})
    if invoice.get("tax_amount") is not None and po_lines:
        actual_tax = D(invoice.get("tax_amount"))
        if not policy.accepts(expected_tax, actual_tax):
            financial_variance = True
            discrepancies.append({"type": "tax_variance", "expected": str(money(expected_tax)), "actual": str(actual_tax), "difference": str(actual_tax - expected_tax), "rule": "Tax is compared to PO line tax rates; no jurisdictional tax advice is implied"})
        elif actual_tax != expected_tax:
            discrepancies.append({"type": "tax_within_tolerance", "expected": str(money(expected_tax)), "actual": str(actual_tax), "difference": str(actual_tax - expected_tax), "rule": "Tax rounding variance accepted within configured tolerance"})

    subtotal_value = D(invoice.get("subtotal"), default=str(invoice_lines_sum))
    tax_value = D(invoice.get("tax_amount"), default=str(expected_tax))
    shipping_value = D(invoice.get("shipping_amount"))
    discount_value = D(invoice.get("discount_amount"))
    invoice_total = invoice.get("total_amount")
    expected_total_from_fields = subtotal_value + tax_value + shipping_value - discount_value
    if invoice_total is not None and not policy.accepts(expected_total_from_fields, D(invoice_total)):
        financial_variance = True
        discrepancies.append({"type": "invoice_total_arithmetic", "expected": str(money(expected_total_from_fields)), "actual": str(D(invoice_total)), "difference": str(D(invoice_total) - expected_total_from_fields), "rule": "Subtotal + tax + shipping - discount must equal invoice total"})
    elif invoice_total is not None and D(invoice_total) != expected_total_from_fields:
        discrepancies.append({"type": "invoice_total_within_tolerance", "expected": str(money(expected_total_from_fields)), "actual": str(D(invoice_total)), "difference": str(D(invoice_total) - expected_total_from_fields), "rule": "Invoice total rounding variance accepted within configured tolerance"})

    all_line_matches = bool(invoice.get("lines")) and all(result["status"] in {"matched", "within_tolerance"} for result in line_results) and not needs_manual
    if signals:
        outcome = "MISMATCH"
    elif any(item["type"] in {"quantity_above_received", "quantity_above_ordered", "cumulative_quantity_above_received", "cumulative_quantity_above_ordered", "unit_price_variance", "currency_mismatch", "vendor_mismatch", "po_closed", "po_cancelled", "line_arithmetic", "unmatched_invoice_line", "no_receipt_lines"} for item in discrepancies):
        outcome = "MISMATCH"
    elif needs_manual:
        outcome = "MANUAL_REVIEW"
    elif not all_line_matches:
        outcome = "MANUAL_REVIEW"
    elif financial_variance:
        outcome = "MISMATCH"
    elif partial_delivery:
        outcome = "PARTIAL_MATCH"
    elif discrepancies:
        outcome = "MATCHED_WITHIN_TOLERANCE"
    else:
        outcome = "MATCHED"

    # Financial exposure is a deterministic estimate of flagged excess pricing/quantity; not a loss claim.
    exposure = Decimal("0")
    quantity_excess_by_line: dict[int, Decimal] = {}
    for item in discrepancies:
        if item["type"] == "unit_price_variance":
            line = next((x for x in line_results if x.get("po_line_number") == item.get("line")), None)
            if line:
                exposure += max(Decimal("0"), D(item["difference"])) * D(line.get("invoice_quantity"))
        elif item["type"] in {"quantity_above_received", "cumulative_quantity_above_received", "cumulative_quantity_above_ordered", "quantity_above_ordered"}:
            try:
                line_number = int(item["line"])
            except (KeyError, TypeError, ValueError):
                continue
            excess = max(Decimal("0"), D(item["difference"]))
            quantity_excess_by_line[line_number] = max(quantity_excess_by_line.get(line_number, Decimal("0")), excess)
    # A single excess unit can violate both ordered and received limits; count its exposure once.
    for line_number, excess in quantity_excess_by_line.items():
        po_line = next((x for x in po_lines if int(x.get("line_number", -1)) == line_number), None)
        if po_line:
            exposure += excess * D(po_line.get("unit_price"))
    if not exposure and outcome in {"MISMATCH", "MISSING_PO", "MISSING_GRN"}:
        exposure = max(Decimal("0"), D(invoice_total))
    return {
        "outcome": outcome,
        "explanation": _explain_outcome(outcome, len(discrepancies), partial_delivery),
        "line_results": line_results,
        "discrepancies": discrepancies,
        "policy": {"absolute_tolerance": str(policy.absolute), "percentage_tolerance": str(policy.percent), "mode": policy.mode},
        "partial_receipt": partial_delivery,
        "expected_received_goods_value": str(money(extended_expected)),
        "financial_exposure": str(money(exposure)),
        "fraud_control_note": "A match outcome is independent of fraud review and cannot authorize payment by itself.",
    }


def _explain_outcome(outcome: str, discrepancy_count: int, partial: bool) -> str:
    if outcome == "MATCHED":
        return "Invoice lines, vendor, currency, receipts, and arithmetic matched the linked records under the configured policy. Fraud analysis remains independent."
    if outcome == "MATCHED_WITHIN_TOLERANCE":
        return f"Records reconcile with {discrepancy_count} documented rounding or financial variance(s) within the configured tolerance."
    if outcome == "PARTIAL_MATCH":
        return "The invoiced quantities reconcile to goods received, while the PO still has undelivered quantity. This is a valid partial-receipt state and is not classified as fraud."
    if outcome == "MANUAL_REVIEW":
        return "Line similarity or source data confidence is not sufficient for automatic reconciliation; reviewer confirmation is required."
    return f"Three-way matching found {discrepancy_count} exception(s). See each expected/actual value and rule in the discrepancy list."
