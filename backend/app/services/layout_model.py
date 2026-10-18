from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import re
from typing import Any


class LayoutModelUnavailable(RuntimeError):
    """The optional locally installed layout-model runtime/checkpoint is not usable."""


ENTITY_TO_FIELD = {
    "VENDOR": "vendor_name", "VENDOR_NAME": "vendor_name", "SUPPLIER": "vendor_name", "SUPPLIER_NAME": "vendor_name",
    "VENDOR_TAX_ID": "vendor_tax_id", "TAX_ID": "vendor_tax_id", "TAX_IDENTIFIER": "vendor_tax_id",
    "INVOICE_NUMBER": "invoice_number", "INVOICE_ID": "invoice_number",
    "PO_NUMBER": "purchase_order_number", "PURCHASE_ORDER": "purchase_order_number", "PURCHASE_ORDER_NUMBER": "purchase_order_number",
    "INVOICE_DATE": "issue_date", "ISSUE_DATE": "issue_date", "DUE_DATE": "due_date", "CURRENCY": "currency",
    "SUBTOTAL": "subtotal", "TAX": "tax_amount", "TAX_AMOUNT": "tax_amount",
    "FREIGHT": "shipping_amount", "SHIPPING": "shipping_amount", "SHIPPING_AMOUNT": "shipping_amount",
    "DISCOUNT": "discount_amount", "DISCOUNT_AMOUNT": "discount_amount",
    "TOTAL": "total_amount", "TOTAL_AMOUNT": "total_amount", "INVOICE_TOTAL": "total_amount", "AMOUNT_DUE": "total_amount",
}


@dataclass(frozen=True)
class TokenLabel:
    label: str
    confidence: float


def normalize_bbox_to_layoutlm(token: dict[str, Any]) -> list[int]:
    """Convert an original page-pixel/point box to LayoutLM's 0..1000 coordinates."""
    box, size = token.get("bbox"), token.get("page_size")
    if not isinstance(box, (list, tuple)) or len(box) != 4 or not isinstance(size, (list, tuple)) or len(size) != 2:
        return [0, 0, 0, 0]
    try:
        width, height = float(size[0]), float(size[1])
        x0, y0, x1, y1 = (float(value) for value in box)
    except (TypeError, ValueError, OverflowError):
        return [0, 0, 0, 0]
    if width <= 0 or height <= 0:
        return [0, 0, 0, 0]
    coords = [round(1000 * x0 / width), round(1000 * y0 / height), round(1000 * x1 / width), round(1000 * y1 / height)]
    coords = [max(0, min(1000, coordinate)) for coordinate in coords]
    coords[2], coords[3] = max(coords[0], coords[2]), max(coords[1], coords[3])
    return coords


def _split_tag(label: str) -> tuple[str, str | None]:
    label = (label or "").strip()
    if not label or label.upper() == "O":
        return "O", None
    match = re.match(r"^(B|I|E|S|U|L)[-_](.+)$", label, re.IGNORECASE)
    prefix, entity = (match.group(1).upper(), match.group(2)) if match else ("B", label)
    normalized = re.sub(r"[^A-Z0-9]+", "_", entity.upper()).strip("_")
    return prefix, ENTITY_TO_FIELD.get(normalized)


def extract_entities(tokens: list[dict[str, Any]], predictions: list[TokenLabel | dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge BIO token predictions into source-grounded field spans without calibrating scores."""
    if len(tokens) != len(predictions):
        raise ValueError("Each source token must have exactly one model prediction")
    spans: list[list[tuple[dict[str, Any], float, str]]] = []
    active: list[tuple[dict[str, Any], float, str]] = []
    active_field: str | None = None
    active_page: Any = None

    def flush() -> None:
        nonlocal active, active_field, active_page
        if active and active_field:
            spans.append(active)
        active, active_field, active_page = [], None, None

    for token, raw_prediction in zip(tokens, predictions):
        if isinstance(raw_prediction, TokenLabel):
            label, confidence = raw_prediction.label, raw_prediction.confidence
        else:
            label = str(raw_prediction.get("label", "O"))
            try:
                confidence = float(raw_prediction.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
        prefix, field_name = _split_tag(label)
        page = token.get("page")
        if field_name is None:
            flush()
            continue
        if prefix in {"B", "S", "U"} or field_name != active_field or page != active_page:
            flush()
        active_field, active_page = field_name, page
        active.append((token, max(0.0, min(1.0, confidence)), label))
        if prefix in {"S", "U", "E", "L"}:
            flush()
    flush()

    candidates: dict[str, list[list[tuple[dict[str, Any], float, str]]]] = defaultdict(list)
    for span in spans:
        field_name = _split_tag(span[0][2])[1]
        if field_name:
            candidates[field_name].append(span)
    result: dict[str, dict[str, Any]] = {}
    for field_name, field_spans in candidates.items():
        ranked = sorted(field_spans, key=lambda span: sum(item[1] for item in span) / len(span), reverse=True)
        chosen = ranked[0]
        source_tokens = [item[0] for item in chosen]
        raw_value = " ".join(str(token.get("text", "")).strip() for token in source_tokens).strip()
        if not raw_value:
            continue
        boxes = [token["bbox"] for token in source_tokens if isinstance(token.get("bbox"), (list, tuple)) and len(token["bbox"]) == 4]
        bbox = [min(float(box[0]) for box in boxes), min(float(box[1]) for box in boxes), max(float(box[2]) for box in boxes), max(float(box[3]) for box in boxes)] if boxes else None
        result[field_name] = {
            "value": raw_value, "normalized_value": normalize_entity_value(field_name, raw_value),
            "confidence": round(sum(item[1] for item in chosen) / len(chosen), 4),
            "source_page": source_tokens[0].get("page"), "bbox": bbox,
            "validation_status": "model_candidate_unvalidated", "extraction_method": "layout_token_classification",
            "confidence_basis": "Mean token-classifier softmax score from a locally configured fine-tuned model; uncalibrated and not an accuracy claim.",
            "confidence_source": "layout_model_softmax_uncalibrated", "model_labels": [item[2] for item in chosen],
            "alternative_span_count": max(0, len(ranked) - 1),
        }
    return result


def normalize_entity_value(field_name: str, value: str) -> str:
    text = " ".join(value.replace("\u00a0", " ").split())
    if field_name == "currency":
        return text.upper()
    if field_name in {"subtotal", "tax_amount", "shipping_amount", "discount_amount", "total_amount"}:
        cleaned = text.replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("₹", "")
        cleaned = re.sub(r"\b(?:USD|EUR|GBP|INR|CAD|AUD)\b", "", cleaned, flags=re.IGNORECASE).strip().replace("(", "-").replace(")", "")
        try:
            from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
            return str(Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError):
            return text
    if field_name in {"issue_date", "due_date"}:
        match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if match:
            from datetime import date
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            except ValueError:
                return text
    return text


@lru_cache(maxsize=2)
def _load_local_token_classifier(model_directory: str, device_name: str):
    path = Path(model_directory).expanduser().resolve()
    if not path.is_dir() or not (path / "config.json").is_file():
        raise LayoutModelUnavailable("Configured layout model directory/config.json was not found")
    try:
        import torch
        from transformers import AutoModelForTokenClassification, AutoTokenizer
    except ImportError as exc:
        raise LayoutModelUnavailable("Install backend/requirements-layout-model.txt to load the optional local layout model") from exc
    try:
        tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, use_fast=True, trust_remote_code=False)
        model = AutoModelForTokenClassification.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
    except Exception as exc:
        raise LayoutModelUnavailable(f"Local layout model could not be loaded ({type(exc).__name__})") from None
    if not getattr(tokenizer, "is_fast", False):
        raise LayoutModelUnavailable("The configured layout model needs a fast tokenizer for source-token alignment")
    id2label = getattr(model.config, "id2label", {}) or {}
    if not any(_split_tag(str(label))[1] is not None for label in id2label.values()):
        raise LayoutModelUnavailable("The checkpoint has no recognized invoice entity labels; configure a fine-tuned token classifier")
    if device_name == "auto":
        device_name = "cuda" if torch.cuda.is_available() else "cpu"
    if device_name == "cuda" and not torch.cuda.is_available():
        raise LayoutModelUnavailable("CUDA was requested but is unavailable")
    try:
        device = torch.device(device_name)
        model.to(device)
        model.eval()
    except Exception as exc:
        raise LayoutModelUnavailable(f"Local layout model device initialization failed ({type(exc).__name__})") from None
    model_version = sha256((path / "config.json").read_bytes()).hexdigest()[:12]
    return torch, tokenizer, model, device, model_version


def classify_tokens_with_local_model(
    tokens: list[dict[str, Any]], model_directory: str | Path, *, device: str = "cpu",
    max_sequence_length: int = 512, chunk_words: int = 400,
) -> tuple[list[TokenLabel], str]:
    """Run a local-only, fine-tuned LayoutLM-compatible token classifier on OCR/PDF words."""
    if max_sequence_length < 32 or max_sequence_length > 512:
        raise ValueError("max_sequence_length must be between 32 and 512")
    if chunk_words < 1 or chunk_words > 1000:
        raise ValueError("chunk_words must be between 1 and 1000")
    path = str(Path(model_directory).expanduser().resolve())
    torch, tokenizer, model, device_object, model_version = _load_local_token_classifier(path, device)
    predictions: list[TokenLabel] = [TokenLabel("O", 0.0) for _ in tokens]
    by_page: dict[Any, list[int]] = defaultdict(list)
    for index, token in enumerate(tokens):
        by_page[token.get("page", 0)].append(index)
    for _page, indices in by_page.items():
        for start in range(0, len(indices), chunk_words):
            chunk_indices = indices[start:start + chunk_words]
            words = [str(tokens[index].get("text", "")) or "[UNK]" for index in chunk_indices]
            boxes = [normalize_bbox_to_layoutlm(tokens[index]) for index in chunk_indices]
            try:
                encoded = tokenizer(words, boxes=boxes, is_split_into_words=True, return_tensors="pt", truncation=True, max_length=max_sequence_length)
                word_ids = encoded.word_ids(batch_index=0)
                encoded = {key: value.to(device_object) for key, value in encoded.items()}
                with torch.inference_mode():
                    logits = model(**encoded).logits[0]
                seen_word_ids: set[int] = set()
                for position, word_id in enumerate(word_ids):
                    if word_id is None or word_id in seen_word_ids or word_id >= len(chunk_indices):
                        continue
                    seen_word_ids.add(word_id)
                    probabilities = torch.softmax(logits[position], dim=-1)
                    label_id = int(torch.argmax(probabilities).item())
                    label_map = model.config.id2label
                    label = str(label_map.get(label_id, label_map.get(str(label_id), "O")))
                    predictions[chunk_indices[word_id]] = TokenLabel(label, float(probabilities[label_id].item()))
                if len(seen_word_ids) != len(chunk_indices):
                    raise LayoutModelUnavailable("The model tokenizer truncated source-token alignment; deterministic extraction was retained")
            except LayoutModelUnavailable:
                raise
            except Exception as exc:
                raise LayoutModelUnavailable(f"Local layout inference failed ({type(exc).__name__})") from None
    return predictions, model_version
