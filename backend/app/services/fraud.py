from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Iterable
import math
import re

import networkx as nx
import numpy as np
from scipy.stats import chi2, chi2_contingency
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler
from rapidfuzz import fuzz

from app.config import settings
from app.services.duplicate_embeddings import semantic_similarity_scores


def normalize_invoice_number(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except Exception:
        return None


def _line_item_similarity(first: Iterable[dict[str, Any]] | None, second: Iterable[dict[str, Any]] | None) -> float | None:
    """Deterministic one-to-one similarity for possible duplicate invoice line tables."""
    first_rows = [row for row in (first or []) if isinstance(row, dict)]
    second_rows = [row for row in (second or []) if isinstance(row, dict)]
    if not first_rows or not second_rows:
        return None

    candidates: list[tuple[float, int, int]] = []
    for left_index, left in enumerate(first_rows):
        left_sku = re.sub(r"[^A-Z0-9]", "", str(left.get("sku") or "").upper())
        left_description = " ".join(str(left.get("description") or "").casefold().split())
        for right_index, right in enumerate(second_rows):
            right_sku = re.sub(r"[^A-Z0-9]", "", str(right.get("sku") or "").upper())
            right_description = " ".join(str(right.get("description") or "").casefold().split())
            description_score = float(fuzz.token_set_ratio(left_description, right_description)) if left_description and right_description else (100.0 if left_sku and left_sku == right_sku else 0.0)
            same_sku = bool(left_sku and right_sku and left_sku == right_sku)
            if left_sku and right_sku and not same_sku and description_score < 95:
                continue
            if not same_sku and description_score < 90:
                continue
            numeric_matches = 0
            for key in ("quantity", "unit_price", "line_total"):
                a, b = _amount(left.get(key)), _amount(right.get(key))
                numeric_matches += int(a is not None and b is not None and a == b)
            sku_score = 1.0 if same_sku else (0.5 if not left_sku and not right_sku else 0.25 if not left_sku or not right_sku else 0.0)
            score = 0.55 * description_score / 100.0 + 0.15 * sku_score + 0.30 * numeric_matches / 3.0
            if score >= 0.70:
                candidates.append((score, left_index, right_index))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_left: set[int] = set()
    used_right: set[int] = set()
    matched_score = 0.0
    for score, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        matched_score += score
    return round(matched_score / max(len(first_rows), len(second_rows)), 6)


def detect_duplicates(current: dict[str, Any], history: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    curr_vendor = current.get("vendor_id") or (current.get("vendor_name") or "").strip().lower()
    curr_number = normalize_invoice_number(current.get("invoice_number"))
    curr_amount = _amount(current.get("total_amount"))
    curr_date = _date(current.get("issue_date"))
    current_hash = current.get("document_hash")
    history_rows = list(history)

    # Embeddings are computed only for same-vendor candidates. The model is optional,
    # local-path-only, and failures return None so deterministic rules remain available.
    candidate_indices: list[int] = []
    seen_candidates: set[str] = set()
    for index, past in enumerate(history_rows):
        past_id = past.get("id")
        if past_id == current.get("id") or (past_id is not None and past_id in seen_candidates):
            continue
        if past_id is not None:
            seen_candidates.add(str(past_id))
        past_vendor = past.get("vendor_id") or (past.get("vendor_name") or "").strip().lower()
        if curr_vendor and past_vendor and curr_vendor != past_vendor:
            continue
        candidate_indices.append(index)
    embedding_scores = semantic_similarity_scores(
        current,
        [history_rows[index] for index in candidate_indices],
        model_path=settings.duplicate_embedding_model_path,
    ) if settings.duplicate_embedding_model_path else []
    embedding_by_index = dict(zip(candidate_indices, embedding_scores))
    embedding_threshold = float(settings.duplicate_embedding_similarity_threshold)
    if not math.isfinite(embedding_threshold):
        embedding_threshold = 0.92
    embedding_threshold = max(0.5, min(0.999, embedding_threshold))

    seen: set[str] = set()
    for past_index, past in enumerate(history_rows):
        past_id = past.get("id")
        if past_id == current.get("id") or (past_id is not None and past_id in seen):
            continue
        if past_id is not None:
            seen.add(str(past_id))
        past_vendor = past.get("vendor_id") or (past.get("vendor_name") or "").strip().lower()
        if curr_vendor and past_vendor and curr_vendor != past_vendor:
            continue
        evidence: dict[str, Any] = {"candidate_invoice_id": past_id, "candidate_number": past.get("invoice_number")}
        past_hash = past.get("document_hash")
        past_number = normalize_invoice_number(past.get("invoice_number"))
        past_amount = _amount(past.get("total_amount"))
        past_date = _date(past.get("issue_date"))
        if current_hash and past_hash and current_hash == past_hash:
            results.append({"type": "exact_duplicate_hash", "severity": 1.0, "explanation": "Uploaded document bytes have the same SHA-256 digest as a prior invoice.", "evidence": evidence})
            continue
        if curr_number and past_number and curr_number == past_number:
            results.append({"type": "exact_duplicate_identifier", "severity": 0.98, "explanation": "The same normalized vendor invoice identifier already exists.", "evidence": evidence})
            continue
        same_amount = curr_amount is not None and past_amount is not None and curr_amount == past_amount
        date_close = curr_date is None or past_date is None or abs((curr_date - past_date).days) <= 60
        similarity = float(fuzz.ratio(curr_number, past_number)) if curr_number and past_number else 0.0
        line_similarity = _line_item_similarity(current.get("line_items"), past.get("line_items"))
        line_content_matches = line_similarity is not None and line_similarity >= 0.85
        embedding_similarity = embedding_by_index.get(past_index)
        embedding_matches = embedding_similarity is not None and embedding_similarity >= embedding_threshold
        if same_amount and date_close and (similarity >= 72 or line_content_matches or embedding_matches):
            methods = []
            if similarity >= 72:
                methods.append("similar normalized invoice identifier")
            if line_content_matches:
                methods.append("near-identical line-item content")
            if embedding_matches:
                methods.append("local sentence-embedding cosine similarity")
            results.append({
                "type": "near_duplicate", "severity": 0.82,
                "explanation": "Same vendor and exact amount within a 60-day window with " + " and ".join(methods) + ". This is an investigation candidate, not a fraud conclusion.",
                "evidence": {
                    **evidence, "invoice_number_similarity": round(similarity, 1),
                    "line_item_similarity": line_similarity,
                    "line_item_counts": [len(current.get("line_items") or []), len(past.get("line_items") or [])],
                    "embedding_cosine_similarity": embedding_similarity if embedding_similarity is not None else None,
                    "embedding_threshold": embedding_threshold if embedding_similarity is not None else None,
                    "amount": str(curr_amount),
                    "days_apart": abs((curr_date - past_date).days) if curr_date and past_date else None,
                },
            })
    return results


def vendor_behavior_features(current: dict[str, Any], history: Iterable[dict[str, Any]]) -> dict[str, Any]:
    current_date = _date(current.get("issue_date"))
    current_amount = float(_amount(current.get("total_amount")) or 0)
    if current_date is None:
        return {
            "history_count": 0, "current_amount": round(current_amount, 2),
            "vendor_median_amount": None, "amount_to_median_ratio": None,
            "invoices_in_prior_7_days": 0, "days_since_previous_invoice": None,
            "indicators": [], "minimum_history_met": False,
            "issue_date_available": False,
        }
    vendor_id = current.get("vendor_id")
    prior = [
        item for item in history
        if item.get("id") != current.get("id")
        and (not vendor_id or item.get("vendor_id") == vendor_id)
        and _date(item.get("issue_date")) is not None
        and _date(item.get("issue_date")) < current_date
    ]
    amounts = [float(_amount(item.get("total_amount")) or 0) for item in prior if _amount(item.get("total_amount")) is not None]
    median = float(np.median(amounts)) if amounts else None
    same_vendor_last_7d = [
        item for item in history
        if item.get("id") != current.get("id")
        and (not vendor_id or item.get("vendor_id") == vendor_id)
        and _date(item.get("issue_date")) is not None
        and 0 <= (current_date - _date(item.get("issue_date"))).days <= 7
    ]
    prior_dates = sorted(_date(item["issue_date"]) for item in prior if _date(item.get("issue_date")) is not None)
    days_since_previous = (current_date - prior_dates[-1]).days if prior_dates else None
    ratio = (current_amount / median) if median and median > 0 else None
    alerts: list[str] = []
    if len(prior) >= 5 and ratio is not None and ratio >= 3:
        alerts.append("invoice_amount_at_least_3x_vendor_median")
    if len(same_vendor_last_7d) >= 3:
        alerts.append("billing_burst_4_or_more_within_7_days")
    if current_amount > 0 and current.get("approval_threshold"):
        threshold = float(current["approval_threshold"])
        if 0 < (threshold - current_amount) <= max(1.0, threshold * 0.01):
            alerts.append("amount_just_below_approval_threshold")
    return {
        "history_count": len(prior),
        "current_amount": round(current_amount, 2),
        "vendor_median_amount": round(median, 2) if median is not None else None,
        "amount_to_median_ratio": round(ratio, 3) if ratio is not None else None,
        "invoices_in_prior_7_days": len(same_vendor_last_7d),
        "days_since_previous_invoice": days_since_previous,
        "indicators": alerts,
        "minimum_history_met": len(prior) >= 5,
        "issue_date_available": True,
    }


def first_significant_digit(value: float | Decimal | int) -> int | None:
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number == 0:
        return None
    while number < 1:
        number *= 10
    while number >= 10:
        number /= 10
    return int(number)


def benford_analysis(
    amounts: Iterable[float | Decimal], *, min_sample: int = 110,
    min_range_orders: float = 2.0, alpha: float = 0.05,
) -> dict[str, Any]:
    values = [float(value) for value in amounts if value is not None and math.isfinite(float(value)) and float(value) > 0]
    digits = [first_significant_digit(value) for value in values]
    digits = [digit for digit in digits if digit is not None]
    counts = Counter(digits)
    sample_size = len(digits)
    observed = [counts.get(digit, 0) for digit in range(1, 10)]
    expected_probabilities = [math.log10(1 + 1 / digit) for digit in range(1, 10)]
    expected_counts = [sample_size * probability for probability in expected_probabilities]
    unique_values = len(set(values))
    ratio = max(values) / min(values) if values and min(values) > 0 else 0.0
    suitable = sample_size >= min_sample and ratio >= 10 ** min_range_orders and unique_values >= 25 and min(expected_counts or [0]) >= 5
    base = {
        "eligible": bool(suitable), "sample_size": sample_size,
        "minimum_sample_size": min_sample, "observed_counts": observed,
        "expected_probabilities": [round(value, 5) for value in expected_probabilities],
        "expected_counts": [round(value, 2) for value in expected_counts],
        "unique_amounts": unique_values, "max_to_min_ratio": round(ratio, 3) if ratio else 0,
        "alpha": alpha, "test": "Pearson chi-square goodness-of-fit on first significant digit",
        "assumptions": ["naturally occurring positive values", "sufficient sample size", "broad value range", "not assigned identifiers or a narrow/threshold-constrained range"],
    }
    if not suitable:
        reasons = []
        if sample_size < min_sample:
            reasons.append(f"requires at least {min_sample} usable values")
        if ratio < 10 ** min_range_orders:
            reasons.append(f"value range must span at least {min_range_orders:g} orders of magnitude")
        if unique_values < 25:
            reasons.append("fewer than 25 unique amounts")
        if min(expected_counts or [0]) < 5:
            reasons.append("expected digit counts are too small for the chi-square approximation")
        return {**base, "tested": False, "flagged": False, "chi_square": None, "p_value": None, "explanation": "Benford screening skipped: " + "; ".join(reasons) + ". A deviation is a screening signal, never standalone evidence of fraud."}
    statistic = sum((obs - exp) ** 2 / exp for obs, exp in zip(observed, expected_counts))
    p_value = float(chi2.sf(statistic, df=8))
    return {
        **base, "tested": True, "flagged": p_value < alpha,
        "chi_square": round(float(statistic), 4), "p_value": round(p_value, 6),
        "explanation": "Significant first-digit deviation; interpret only after confirming the dataset is suitable. This is not a fraud probability or conclusion." if p_value < alpha else "No statistically significant first-digit deviation at the configured alpha; this does not establish that transactions are legitimate.",
    }


def build_behavior_vector(
    amount: float, amount_ratio: float, count_7d: float,
    days_since_previous: float, price_variance_ratio: float, po_lag_days: float,
) -> list[float]:
    return [
        float(np.log1p(max(0.0, amount))),
        float(np.clip(amount_ratio, 0.0, 100.0)),
        float(np.clip(count_7d, 0.0, 100.0)),
        float(np.clip(days_since_previous, 0.0, 3650.0)),
        float(np.clip(price_variance_ratio, -10.0, 10.0)),
        float(np.clip(po_lag_days, -3650.0, 3650.0)),
    ]


def train_isolation_forest(
    features: list[list[float]] | np.ndarray, *, contamination: float = 0.05,
    random_state: int = 42, min_samples: int = 20,
) -> tuple[Pipeline | None, dict[str, Any]]:
    array = np.asarray(features, dtype=float)
    if array.ndim != 2 or array.shape[1] != 6 or array.shape[0] < min_samples or not np.isfinite(array).all():
        return None, {"trained": False, "reason": f"Need at least {min_samples} finite six-feature historical examples.", "training_rows": int(array.shape[0]) if array.ndim >= 1 else 0}
    model = Pipeline([
        ("robust_scaler", RobustScaler(quantile_range=(10, 90))),
        ("isolation_forest", IsolationForest(n_estimators=200, contamination=contamination, random_state=random_state, n_jobs=1)),
    ])
    model.fit(array)
    return model, {"trained": True, "training_rows": int(array.shape[0]), "contamination_assumption": contamination, "random_state": random_state, "feature_names": ["log_amount", "amount_to_vendor_median", "prior_7d_count", "days_since_previous", "price_variance_ratio", "po_lag_days"], "score_semantics": "unsupervised anomaly score; not calibrated fraud probability"}


def score_isolation_forest(model: Pipeline, current_features: list[float], reference_features: list[list[float]] | np.ndarray | None = None) -> dict[str, Any]:
    vector = np.asarray(current_features, dtype=float).reshape(1, -1)
    raw_score = float(model.decision_function(vector)[0])
    predicted_outlier = int(model.predict(vector)[0]) == -1
    if reference_features is not None and len(reference_features):
        reference_scores = np.asarray(model.decision_function(np.asarray(reference_features, dtype=float)))
        lower, upper = np.quantile(reference_scores, [0.01, 0.99])
        severity = float(np.clip((upper - raw_score) / max(upper - lower, 1e-9), 0.0, 1.0))
    else:
        severity = 0.6 if predicted_outlier else 0.1
    return {
        "anomaly": predicted_outlier, "decision_score": round(raw_score, 6),
        "severity_index": round(severity, 4), "is_probability": False,
        "explanation": "Isolation Forest ranks this behavior as atypical relative to the selected reference history; score is not a fraud probability.",
    }


def relationship_overlaps(vendors: Iterable[dict[str, Any]], employees: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build an in-memory token graph and return vendor/vendor and vendor/employee overlaps.

    Sensitive account/address values are expected to be keyed tokens. They are used only
    for equality joins and are never included in findings. This is a screening heuristic,
    not a validated phantom-vendor classifier or persistent graph database.
    """
    vendor_rows = [row for row in vendors if isinstance(row, dict) and row.get("id") is not None]
    employee_rows = [row for row in employees if isinstance(row, dict) and row.get("id") is not None]
    graph = nx.Graph()
    for vendor in vendor_rows:
        graph.add_node(f"vendor:{vendor['id']}", kind="vendor", name=vendor.get("name", ""))
    for employee in employee_rows:
        graph.add_node(f"employee:{employee['id']}", kind="employee", name=employee.get("display_name", ""))

    token_fields = ("address_token", "bank_token")
    vendor_token_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for vendor in vendor_rows:
        for field in token_fields:
            token = vendor.get(field)
            if token:
                vendor_token_groups[(field, str(token))].append(vendor)
    vendor_pair_fields: dict[tuple[str, str], set[str]] = defaultdict(set)
    vendor_pair_sizes: dict[tuple[str, str], int] = {}
    for (field, _token), group in vendor_token_groups.items():
        if len(group) < 2:
            continue
        # A deterministic spanning star preserves connectivity while avoiding quadratic
        # edge growth for large shared-account/address groups.
        hub_id = str(group[0]["id"])
        for member in group[1:]:
            member_id = str(member["id"])
            if member_id == hub_id:
                continue
            pair = tuple(sorted((hub_id, member_id)))
            vendor_pair_fields[pair].add(field)
            vendor_pair_sizes[pair] = max(vendor_pair_sizes.get(pair, 0), len(group))
    for (first_id, second_id), overlap_types in sorted(vendor_pair_fields.items()):
        graph.add_edge(f"vendor:{first_id}", f"vendor:{second_id}", relationship="shared_sensitive_token", fields=sorted(overlap_types))
    findings: list[dict[str, Any]] = []
    for (first_id, second_id), overlap_types in sorted(vendor_pair_fields.items()):
        findings.append({
            "type": "vendor_vendor_relationship_overlap",
            "severity": 0.88,
            "explanation": "Vendor records share a protected bank/address token; independently verify ownership. No raw bank or address values are returned.",
            "evidence": {
                "vendor_id": first_id,
                "related_vendor_id": second_id,
                "overlap_types": sorted(overlap_types),
                "shared_token_group_size": vendor_pair_sizes[(first_id, second_id)],
                "graph_degree": graph.degree(f"vendor:{first_id}"),
            },
        })

    for vendor in vendor_rows:
        for employee in employee_rows:
            overlaps = [field for field in token_fields if vendor.get(field) and vendor.get(field) == employee.get(field)]
            if overlaps:
                source = f"vendor:{vendor['id']}"
                target = f"employee:{employee['id']}"
                graph.add_edge(source, target, relationship="shared_sensitive_token", fields=overlaps)
                findings.append({
                    "type": "vendor_employee_relationship_overlap", "severity": 0.95,
                    "explanation": "Vendor and employee records share a protected relationship token; independently verify the relationship. No raw bank or address values are returned.",
                    "evidence": {"vendor_id": vendor["id"], "employee_id": employee["id"], "overlap_types": overlaps, "graph_degree": graph.degree(source)},
                })
    return findings


def aggregate_heuristic_risk(signals: Iterable[dict[str, Any]]) -> tuple[float, str]:
    """Combine detector severities into a prioritization index, explicitly not a probability."""
    score = 0.0
    for signal in signals:
        severity = float(np.clip(signal.get("severity", 0.0), 0.0, 1.0))
        weight = float(np.clip(signal.get("weight", 1.0), 0.0, 1.0))
        score = 1.0 - (1.0 - score) * (1.0 - severity * weight)
    score = round(float(np.clip(score, 0.0, 1.0)), 4)
    band = "critical" if score >= 0.85 else "high" if score >= 0.6 else "medium" if score >= 0.25 else "low"
    return score, band


def evaluate_binary_predictions(
    y_true: Iterable[int], y_score: Iterable[float], *, k: int = 20, threshold: float = 0.5,
) -> dict[str, Any]:
    """Score a supplied held-out labeled cohort; output is evaluation, never a training step."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    try:
        truth_values = np.asarray(list(y_true), dtype=float)
        scores = np.asarray(list(y_score), dtype=float)
        threshold_value = float(threshold)
    except (TypeError, ValueError, OverflowError):
        return {"evaluated": False, "reason": "Labels, scores, and threshold must be numeric."}
    if k < 1:
        return {"evaluated": False, "reason": "Review capacity k must be positive."}
    if not math.isfinite(threshold_value) or not 0 <= threshold_value <= 1:
        return {"evaluated": False, "reason": "Threshold must be finite and between 0 and 1."}
    if truth_values.ndim != 1 or scores.ndim != 1 or len(truth_values) == 0 or len(truth_values) != len(scores):
        return {"evaluated": False, "reason": "Need non-empty, aligned one-dimensional labels and scores."}
    if not np.isfinite(truth_values).all() or not np.isin(truth_values, [0, 1]).all():
        return {"evaluated": False, "reason": "Labels must be finite binary values 0/1."}
    if not np.isfinite(scores).all() or ((scores < 0) | (scores > 1)).any():
        return {"evaluated": False, "reason": "Scores must be finite ranking values between 0 and 1."}
    truth = truth_values.astype(int)
    if len(np.unique(truth)) < 2:
        return {"evaluated": False, "reason": "Need aligned labels containing both classes; no metrics were fabricated."}

    prediction = scores >= threshold_value
    positives = truth == 1
    negatives = ~positives
    true_positive = int(np.sum(prediction & positives))
    false_positive = int(np.sum(prediction & negatives))
    false_negative = int(np.sum(~prediction & positives))
    true_negative = int(np.sum(~prediction & negatives))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    specificity = true_negative / (true_negative + false_positive) if true_negative + false_positive else 0.0
    fpr = false_positive / (false_positive + true_negative) if false_positive + true_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    top = np.argsort(-scores, kind="stable")[: min(k, len(scores))]
    hits_at_k = int(truth[top].sum())
    positives_total = int(positives.sum())
    precision_at_k = hits_at_k / len(top) if len(top) else 0.0
    recall_at_k = hits_at_k / positives_total if positives_total else 0.0
    return {
        "evaluated": True,
        "n": int(len(truth)),
        "positive_count": positives_total,
        "negative_count": int(negatives.sum()),
        "threshold": threshold_value,
        "precision_at_threshold": round(precision, 4),
        "recall_at_threshold": round(recall, 4),
        "f1_at_threshold": round(f1, 4),
        "false_positive_rate": round(fpr, 4),
        "specificity": round(specificity, 4),
        "confusion_matrix": {"true_positive": true_positive, "false_positive": false_positive, "true_negative": true_negative, "false_negative": false_negative},
        "average_precision": round(float(average_precision_score(truth, scores)), 4),
        "roc_auc": round(float(roc_auc_score(truth, scores)), 4),
        "review_capacity_k": int(len(top)),
        "precision_at_review_capacity": round(precision_at_k, 4),
        "recall_at_review_capacity": round(recall_at_k, 4),
        "label_semantics": "Only valid for independently reviewed labels from the documented held-out cohort; synthetic or case-enriched labels do not establish real-world performance.",
    }
