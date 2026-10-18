from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
from typing import Any, Iterable


SCALAR_FIELDS = (
    "vendor_name", "vendor_tax_id", "invoice_number", "purchase_order_number",
    "issue_date", "due_date", "currency", "subtotal", "tax_amount",
    "shipping_amount", "discount_amount", "total_amount",
)
MONEY_FIELDS = {"subtotal", "tax_amount", "shipping_amount", "discount_amount", "total_amount", "unit_price", "line_total"}
NUMERIC_FIELDS = MONEY_FIELDS | {"quantity"}
LINE_FIELDS = ("sku", "description", "quantity", "unit_price", "line_total")


def _unwrap(value: Any) -> tuple[Any, float | None]:
    """Accept either plain gold/prediction values or the application's field payloads."""
    if isinstance(value, dict) and "value" in value:
        raw_value = value.get("value")
        confidence = value.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
            confidence = None
        return raw_value, confidence
    return value, None


def _present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _decimal(value: Any, *, money: bool = False) -> Decimal | None:
    if not _present(value) or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    text = re.sub(r"\b(?:USD|EUR|GBP|INR|CAD|AUD)\b", "", text, flags=re.IGNORECASE)
    text = text.replace("$", "").replace("€", "").replace("£", "").replace("₹", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = Decimal(text)
        if not number.is_finite():
            return None
        return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if money else number.normalize()
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip() if _present(value) else ""
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalize(field: str, value: Any) -> str | Decimal | date | None:
    if not _present(value):
        return None
    if field in NUMERIC_FIELDS:
        parsed = _decimal(value, money=field in MONEY_FIELDS)
        return parsed if parsed is not None else "invalid:" + " ".join(str(value).split()).casefold()
    if field in {"issue_date", "due_date"}:
        return _date(value) or " ".join(str(value).split()).casefold()
    text = " ".join(str(value).replace("\u00a0", " ").split()).casefold()
    if field in {"invoice_number", "purchase_order_number", "vendor_tax_id"}:
        return re.sub(r"[^a-z0-9]", "", text)
    if field == "currency":
        return text.upper()
    return text


def _exact_key(field: str, value: Any) -> Any:
    if not _present(value):
        return None
    if field in NUMERIC_FIELDS:
        parsed = _decimal(value, money=field in MONEY_FIELDS)
        return parsed if parsed is not None else "invalid:" + " ".join(str(value).split())
    if field in {"issue_date", "due_date"}:
        parsed = _date(value)
        return parsed if parsed else " ".join(str(value).split())
    return " ".join(str(value).replace("\u00a0", " ").split())


def _field_metrics(gold_values: list[Any], predicted_values: list[Any], field: str) -> dict[str, Any]:
    count = len(gold_values)
    tp = fp = fn = exact_count = normalized_count = 0
    numeric_errors: list[Decimal] = []
    confidence_samples: list[tuple[float, int]] = []
    gold_present = predicted_present = 0
    for gold_raw, predicted_raw in zip(gold_values, predicted_values):
        gold, _ = _unwrap(gold_raw)
        predicted, confidence = _unwrap(predicted_raw)
        has_gold, has_prediction = _present(gold), _present(predicted)
        gold_present += int(has_gold)
        predicted_present += int(has_prediction)
        exact = _exact_key(field, gold) == _exact_key(field, predicted)
        normalized = _normalize(field, gold) == _normalize(field, predicted)
        exact_count += int(exact)
        normalized_count += int(normalized)
        is_tp = has_gold and has_prediction and normalized
        tp += int(is_tp)
        fp += int(has_prediction and not is_tp)
        fn += int(has_gold and not is_tp)
        if has_gold and has_prediction and field in NUMERIC_FIELDS:
            gold_number = _decimal(gold, money=field in MONEY_FIELDS)
            predicted_number = _decimal(predicted, money=field in MONEY_FIELDS)
            if gold_number is not None and predicted_number is not None:
                numeric_errors.append(abs(predicted_number - gold_number))
        if has_prediction and confidence is not None:
            confidence_samples.append((confidence, int(normalized and has_gold)))

    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else (0.0 if precision == 0 or recall == 0 else None)
    exact_present = sum(
        int(_present(_unwrap(gold)[0]) and _exact_key(field, _unwrap(gold)[0]) == _exact_key(field, _unwrap(pred)[0]))
        for gold, pred in zip(gold_values, predicted_values)
    )
    return {
        "documents": count,
        "gold_present": gold_present,
        "predicted_present": predicted_present,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "exact_match_accuracy_on_gold_present": round(exact_present / gold_present, 6) if gold_present else None,
        "exact_match_accuracy_all_documents": round(exact_count / count, 6) if count else None,
        "normalized_match_accuracy_all_documents": round(normalized_count / count, 6) if count else None,
        "numeric_mae": round(float(sum(numeric_errors) / len(numeric_errors)), 6) if numeric_errors else None,
        "numeric_pairs": len(numeric_errors),
        "confidence_diagnostics": _confidence_diagnostics(confidence_samples),
    }


def _confidence_diagnostics(samples: list[tuple[float, int]], bins: int = 10) -> dict[str, Any]:
    if not samples:
        return {"sample_size": 0, "brier_score": None, "expected_calibration_error": None, "bins": []}
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for confidence, correct in samples:
        index = min(bins - 1, int(confidence * bins))
        buckets[index].append((confidence, correct))
    brier = sum((confidence - correct) ** 2 for confidence, correct in samples) / len(samples)
    ece = 0.0
    output_bins = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        mean_confidence = sum(item[0] for item in bucket) / len(bucket)
        accuracy = sum(item[1] for item in bucket) / len(bucket)
        ece += len(bucket) / len(samples) * abs(mean_confidence - accuracy)
        output_bins.append({
            "lower_bound": round(index / bins, 2),
            "upper_bound": round((index + 1) / bins, 2),
            "count": len(bucket),
            "mean_confidence": round(mean_confidence, 6),
            "observed_accuracy": round(accuracy, 6),
        })
    return {
        "sample_size": len(samples),
        "brier_score": round(brier, 6),
        "expected_calibration_error": round(ece, 6),
        "bins": output_bins,
        "interpretation": "Descriptive diagnostics only; this report does not calibrate confidence or make an uncalibrated score a probability.",
    }


def _line_identity(line: dict[str, Any]) -> tuple[str, str]:
    sku = " ".join(str(line.get("sku") or "").upper().split())
    description = " ".join(str(line.get("description") or "").casefold().split())
    return sku, description


def _line_score(gold: dict[str, Any], predicted: dict[str, Any]) -> float | None:
    from rapidfuzz.fuzz import token_set_ratio

    gold_sku, gold_description = _line_identity(gold)
    predicted_sku, predicted_description = _line_identity(predicted)
    if not gold_description or not predicted_description:
        if not gold_sku or gold_sku != predicted_sku:
            return None
        description_score = 100.0
    else:
        description_score = float(token_set_ratio(gold_description, predicted_description))
    same_sku = bool(gold_sku and predicted_sku and gold_sku == predicted_sku)
    if gold_sku and predicted_sku and not same_sku and description_score < 95:
        return None
    if not same_sku and description_score < 90:
        return None
    numeric_matches = 0
    numeric_fields = ("quantity", "unit_price", "line_total")
    for field in numeric_fields:
        a = _decimal(gold.get(field), money=field in MONEY_FIELDS)
        b = _decimal(predicted.get(field), money=field in MONEY_FIELDS)
        numeric_matches += int(a is not None and b is not None and a == b)
    sku_score = 1.0 if same_sku else (0.5 if not gold_sku and not predicted_sku else 0.0)
    return 0.55 * (description_score / 100.0) + 0.15 * sku_score + 0.30 * (numeric_matches / 3.0)


def _match_lines(gold_rows: list[dict[str, Any]], predicted_rows: list[dict[str, Any]]) -> list[tuple[int, int, float]]:
    candidates = []
    for gold_index, gold in enumerate(gold_rows):
        for predicted_index, predicted in enumerate(predicted_rows):
            score = _line_score(gold, predicted)
            if score is not None and score >= 0.70:
                candidates.append((score, gold_index, predicted_index))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_gold: set[int] = set()
    used_predicted: set[int] = set()
    matched = []
    for score, gold_index, predicted_index in candidates:
        if gold_index in used_gold or predicted_index in used_predicted:
            continue
        used_gold.add(gold_index)
        used_predicted.add(predicted_index)
        matched.append((gold_index, predicted_index, score))
    return matched


def _line_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    gold_count = predicted_count = matched_count = exact_row_count = exact_document_count = 0
    matched_rows = 0
    numeric_errors: dict[str, list[Decimal]] = defaultdict(list)
    for record in records:
        gold = record["gold"].get("line_items", []) or []
        predicted = record["prediction"].get("line_items", []) or []
        if not isinstance(gold, list) or not isinstance(predicted, list):
            raise ValueError(f"Record {record['id']!r}: line_items must be a list")
        if any(not isinstance(item, dict) for item in [*gold, *predicted]):
            raise ValueError(f"Record {record['id']!r}: each line item must be an object")
        matches = _match_lines(gold, predicted)
        gold_count += len(gold)
        predicted_count += len(predicted)
        matched_count += len(matches)
        row_exact_count = 0
        for gold_index, predicted_index, _score in matches:
            matched_rows += 1
            expected = gold[gold_index]
            observed = predicted[predicted_index]
            required_line_fields = ("description", "quantity", "unit_price", "line_total")
            exact = (
                all(_present(expected.get(field)) for field in required_line_fields)
                and all(_normalize(field, expected.get(field)) == _normalize(field, observed.get(field)) for field in required_line_fields)
                and (not _present(expected.get("sku")) or _normalize("sku", expected.get("sku")) == _normalize("sku", observed.get("sku")))
            )
            row_exact_count += int(exact)
            for field in ("quantity", "unit_price", "line_total"):
                if _present(expected.get(field)) and _present(observed.get(field)):
                    a = _decimal(expected.get(field), money=field in MONEY_FIELDS)
                    b = _decimal(observed.get(field), money=field in MONEY_FIELDS)
                    if a is not None and b is not None:
                        numeric_errors[field].append(abs(a - b))
        exact_row_count += row_exact_count
        exact_document_count += int(len(gold) == len(predicted) and row_exact_count == len(gold))
    precision = matched_count / predicted_count if predicted_count else None
    recall = matched_count / gold_count if gold_count else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else (0.0 if precision == 0 or recall == 0 else None)
    return {
        "gold_rows": gold_count,
        "predicted_rows": predicted_count,
        "matched_rows": matched_count,
        "false_positive_rows": predicted_count - matched_count,
        "false_negative_rows": gold_count - matched_count,
        "row_match_precision": round(precision, 6) if precision is not None else None,
        "row_match_recall": round(recall, 6) if recall is not None else None,
        "row_match_f1": round(f1, 6) if f1 is not None else None,
        "exact_row_match_rate_among_matched": round(exact_row_count / matched_count, 6) if matched_count else None,
        "documents_with_exact_line_set": exact_document_count,
        "exact_line_set_rate": round(exact_document_count / len(records), 6) if records else None,
        "numeric_mae_by_field": {
            field: {
                "mae": round(float(sum(errors) / len(errors)), 6),
                "pairs": len(errors),
            }
            for field, errors in sorted(numeric_errors.items())
        },
        "matching_policy": "One-to-one greedy row alignment by SKU/description similarity, then numeric agreement; not a learned table model.",
    }


def evaluate_extractions(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Score a held-out extraction set; does not fit a model or create labels."""
    dataset = list(records)
    if not dataset:
        raise ValueError("At least one labeled record is required")
    prepared = []
    for index, record in enumerate(dataset, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Record {index} must be a JSON object")
        identifier = record.get("id", str(index))
        gold = record.get("gold")
        prediction = record.get("prediction")
        if not isinstance(gold, dict) or not isinstance(prediction, dict):
            raise ValueError(f"Record {identifier!r} must have object-valued 'gold' and 'prediction' keys")
        prepared.append({"id": str(identifier), "gold": gold, "prediction": prediction})

    field_metrics = {
        field: _field_metrics(
            [record["gold"].get(field) for record in prepared],
            [record["prediction"].get(field) for record in prepared],
            field,
        )
        for field in SCALAR_FIELDS
    }
    return {
        "report_version": "invoice-extraction-eval-v1",
        "documents": len(prepared),
        "field_metrics": field_metrics,
        "line_item_metrics": _line_metrics(prepared),
        "confidence_note": "Source confidence is scored against the supplied labels for diagnostics only; scores are not recalibrated or asserted to be probabilities.",
        "dataset_note": "Metrics describe only the supplied labeled examples. Use an independently adjudicated, vendor/time-held-out corpus; synthetic tests are not production evidence.",
    }
