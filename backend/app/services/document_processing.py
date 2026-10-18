from __future__ import annotations

import io
import math
import re
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import pymupdf as fitz
import numpy as np
from PIL import Image

from app.config import settings
from app.services.docx_processing import read_docx, render_docx_text_preview
from app.services.layout_model import LayoutModelUnavailable, classify_tokens_with_local_model, extract_entities

try:
    import pytesseract
    from pytesseract import Output
except ImportError:  # pragma: no cover - dependency is declared but adapter stays optional
    pytesseract = None
    Output = None


MAX_DOCUMENT_PAGES = 200
MAX_IMAGE_PIXELS = 40_000_000
MAX_RENDERED_PAGE_PIXELS = 40_000_000
PDF_RENDER_SCALE = 4.0


class OCRUnavailable(RuntimeError):
    pass


class OCRTimeoutError(OCRUnavailable):
    """Local OCR exceeded a configured per-call or per-document time budget."""


def _ocr_call_timeout(deadline: float | None) -> float:
    configured = max(1, min(300, int(settings.ocr_timeout_seconds)))
    if deadline is None:
        return float(configured)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OCRTimeoutError("OCR exceeded the configured per-document time budget.")
    return min(float(configured), remaining)


def _check_image_dimensions(data: bytes) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("The image exceeds the safe decompression limit") from exc
    except (OSError, ValueError) as exc:
        raise ValueError("Unable to read image dimensions") from exc
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f"Image dimensions exceed the {MAX_IMAGE_PIXELS:,}-pixel processing limit")
    return width, height


@dataclass
class DocumentExtraction:
    raw_text: str
    tokens: list[dict[str, Any]]
    fields: dict[str, Any]
    validation_errors: list[dict[str, Any]]
    engine_name: str
    engine_version: str
    page_count: int


def tesseract_available() -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def preprocess_image(image: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    """CPU-first grayscale, denoise, optional deskew, and adaptive thresholding."""
    if image is None or image.size == 0:
        raise ValueError("The uploaded image is empty or unreadable")
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()
    height, width = gray.shape[:2]
    denoised = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
    binary_for_angle = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coordinates = np.column_stack(np.where(binary_for_angle > 0))
    rotation_degrees = 0.0
    corrected = denoised
    if len(coordinates) > 100:
        angle = cv2.minAreaRect(coordinates.astype(np.float32))[-1]
        if angle < -45:
            angle = 90 + angle
        rotation_degrees = -float(angle)
        if 0.8 <= abs(rotation_degrees) <= 12:
            center = (width // 2, height // 2)
            matrix = cv2.getRotationMatrix2D(center, rotation_degrees, 1.0)
            corrected = cv2.warpAffine(
                denoised, matrix, (width, height), flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
        else:
            rotation_degrees = 0.0
    thresholded = cv2.adaptiveThreshold(
        corrected, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )
    return thresholded, {
        "width": int(width), "height": int(height),
        "deskew_degrees": round(rotation_degrees, 3),
        "preprocessing": ["grayscale", "non_local_means_denoise", "optional_deskew", "adaptive_threshold"],
    }


def _page_tokens_from_pdf(page: fitz.Page, page_index: int) -> list[dict[str, Any]]:
    words = page.get_text("words", sort=True)
    result = []
    for word in words:
        x0, y0, x1, y1, text = word[:5]
        if text.strip():
            result.append({
                "text": text,
                "page": page_index,
                "bbox": [round(float(x0), 2), round(float(y0), 2), round(float(x1), 2), round(float(y1), 2)],
                "page_size": [round(float(page.rect.width), 2), round(float(page.rect.height), 2)],
                "layout_group": f"pdf:{page_index}:{word[5]}:{word[6]}" if len(word) > 6 else f"pdf:{page_index}:row:{round(float(y0), 1)}",
                "word_order": int(word[7]) if len(word) > 7 else 0,
                "confidence": 1.0,
                "confidence_source": "embedded_pdf_text_not_ocr",
                "source": "pdf_text_layer",
            })
    return result


def _ocr_image(image: np.ndarray, page_index: int, *, deadline: float | None = None) -> tuple[str, list[dict[str, Any]]]:
    if not tesseract_available():
        raise OCRUnavailable("Tesseract is not installed or not on PATH. Install tesseract-ocr to process scanned pages.")
    processed, meta = preprocess_image(image)
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()
    denoised = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
    # Thresholding can erase faint characters. Compare a denoised grayscale pass with the
    # adaptive-threshold pass and retain the actual Tesseract result with stronger evidence.
    candidates = []
    for candidate_image in (denoised, processed):
        for psm in (4, 6):
            timeout = _ocr_call_timeout(deadline)
            try:
                candidate_data = pytesseract.image_to_data(
                    Image.fromarray(candidate_image), output_type=Output.DICT,
                    config=f"--oem 3 --psm {psm}", timeout=timeout,
                )
            except RuntimeError as exc:
                # pytesseract raises this specific message after terminating an over-time process.
                if "tesseract process timeout" in str(exc).lower():
                    raise OCRTimeoutError(
                        f"Tesseract exceeded the {timeout:g}-second per-attempt OCR time limit."
                    ) from None
                raise
            candidate_conf = []
            word_count = 0
            for raw_text, raw_conf in zip(candidate_data.get("text", []), candidate_data.get("conf", [])):
                if str(raw_text).strip():
                    word_count += 1
                    try:
                        score = float(raw_conf)
                        if score >= 0:
                            candidate_conf.append(score)
                    except (TypeError, ValueError):
                        pass
            mean_conf = sum(candidate_conf) / len(candidate_conf) if candidate_conf else 0.0
            quality = mean_conf * min(1.0, word_count / 25.0)
            candidates.append((quality, word_count, candidate_data, psm, candidate_image is processed))
    _, _, data, selected_psm, used_threshold = max(candidates, key=lambda item: (item[0], item[1]))
    meta["selected_psm"] = selected_psm
    meta["selected_preprocessing"] = "adaptive_threshold" if used_threshold else "denoised_grayscale"
    tokens: list[dict[str, Any]] = []
    lines: dict[tuple[int, int, int], list[str]] = {}
    for i, raw in enumerate(data.get("text", [])):
        text = str(raw).strip()
        if not text:
            continue
        try:
            confidence = max(0.0, min(1.0, float(data["conf"][i]) / 100.0))
        except (TypeError, ValueError, KeyError):
            confidence = 0.0
        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        block_number = int(data["block_num"][i])
        paragraph_number = int(data["par_num"][i])
        line_number = int(data["line_num"][i])
        tokens.append({
            "text": text, "page": page_index, "bbox": [x, y, x + w, y + h],
            "page_size": [meta["width"], meta["height"]],
            "layout_group": f"ocr:{page_index}:{block_number}:{paragraph_number}:{line_number}",
            "word_order": int(data.get("word_num", [i] * len(data.get("text", [])))[i]),
            "confidence": round(confidence, 4), "confidence_source": "tesseract_word_confidence",
            "source": "tesseract_ocr",
        })
        key = (block_number, paragraph_number, line_number)
        lines.setdefault(key, []).append(text)
    text = "\n".join(" ".join(words) for words in lines.values())
    return text, tokens


def read_document(data: bytes, filename: str, mime_type: str) -> tuple[str, list[dict[str, Any]], int, str]:
    suffix = Path(filename).suffix.lower()
    tokens: list[dict[str, Any]] = []
    texts: list[str] = []
    engine_parts: set[str] = set()
    document_budget = max(1, min(3600, int(settings.ocr_document_timeout_seconds)))
    ocr_deadline = time.monotonic() + document_budget
    if suffix == ".docx":
        text, tokens = read_docx(data)
        return text, tokens, 1, "docx-ooxml-text"
    if suffix == ".pdf" or mime_type == "application/pdf" or data[:5] == b"%PDF-":
        with fitz.open(stream=data, filetype="pdf") as pdf:
            page_count = len(pdf)
            if page_count < 1:
                raise ValueError("The PDF contains no pages")
            if page_count > MAX_DOCUMENT_PAGES:
                raise ValueError(f"The PDF exceeds the {MAX_DOCUMENT_PAGES}-page processing limit")
            for page_index, page in enumerate(pdf, start=1):
                page_text = page.get_text("text", sort=True).strip()
                page_tokens = _page_tokens_from_pdf(page, page_index)
                if len(page_tokens) >= 3 and len(page_text) >= 15:
                    texts.append(page_text)
                    tokens.extend(page_tokens)
                    engine_parts.add("pymupdf-text")
                    continue
                raster_width = math.ceil(float(page.rect.width) * PDF_RENDER_SCALE)
                raster_height = math.ceil(float(page.rect.height) * PDF_RENDER_SCALE)
                if raster_width <= 0 or raster_height <= 0 or raster_width * raster_height > MAX_RENDERED_PAGE_PIXELS:
                    raise ValueError(f"PDF page {page_index} exceeds the safe raster-processing limit")
                pixmap = page.get_pixmap(matrix=fitz.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE), alpha=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
                ocr_text, ocr_tokens = _ocr_image(image, page_index, deadline=ocr_deadline)
                texts.append(ocr_text)
                tokens.extend(ocr_tokens)
                engine_parts.add("tesseract-ocr")
            return "\n\n".join(texts), tokens, page_count, "+".join(sorted(engine_parts))
    if suffix not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        raise ValueError("Supported documents are PDF, PNG, JPG/JPEG, TIFF, and BMP")
    _check_image_dimensions(data)
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Unable to decode uploaded image")
    text, image_tokens = _ocr_image(image, 1, deadline=ocr_deadline)
    return text, image_tokens, 1, "tesseract-ocr"


def render_document_page(data: bytes, filename: str, mime_type: str, page_number: int) -> bytes:
    """Render a source-page preview, or a clearly reflowed text-only preview for DOCX."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        if page_number != 1:
            raise IndexError("DOCX text preview page not found")
        return render_docx_text_preview(data)
    if suffix == ".pdf" or mime_type == "application/pdf" or data[:5] == b"%PDF-":
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if page_number < 1 or page_number > len(pdf):
                raise IndexError("Document page not found")
            if len(pdf) > MAX_DOCUMENT_PAGES:
                raise ValueError(f"The PDF exceeds the {MAX_DOCUMENT_PAGES}-page preview limit")
            page = pdf[page_number - 1]
            width = math.ceil(float(page.rect.width) * PDF_RENDER_SCALE)
            height = math.ceil(float(page.rect.height) * PDF_RENDER_SCALE)
            if width <= 0 or height <= 0 or width * height > MAX_RENDERED_PAGE_PIXELS:
                raise ValueError(f"PDF page {page_number} exceeds the safe raster-preview limit")
            return page.get_pixmap(matrix=fitz.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE), alpha=False).tobytes("png")
    if page_number != 1:
        raise IndexError("Document page not found")
    _check_image_dimensions(data)
    with Image.open(io.BytesIO(data)) as source:
        preview = source.convert("RGB")
        output = io.BytesIO()
        preview.save(output, format="PNG")
        return output.getvalue()


def _normalise_amount(value: str) -> Decimal | None:
    cleaned = value.strip().replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("USD", "").replace("INR", "")
    cleaned = cleaned.replace("(", "-").replace(")", "")
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: str) -> date | None:
    value = value.strip().replace(",", "")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _line_field(text: str, patterns: list[str]) -> tuple[str | None, str | None]:
    for line in text.splitlines():
        for pattern in patterns:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" \t:#-"), line
    return None, None


def _normalise_layout_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _bbox_for_source_line(source_line: str | None, tokens: list[dict[str, Any]]) -> tuple[int | None, list[float] | None]:
    """Match a parsed text row to OCR/PDF token groups and return the row-level source box."""
    target = _normalise_layout_text(source_line or "")
    if not target:
        return None, None
    groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for token in tokens:
        box = token.get("bbox")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            page = int(token.get("page", 0))
            group_id = token.get("layout_group")
            if not group_id:
                group_id = f"fallback-row-{round(float(box[1]), 1)}"
            groups[(page, str(group_id))].append(token)
        except (TypeError, ValueError):
            continue
    best: tuple[float, int, list[dict[str, Any]]] | None = None
    for (page, _group_id), line_tokens in groups.items():
        ordered = sorted(line_tokens, key=lambda token: (float(token["bbox"][0]), int(token.get("word_order", 0))))
        candidate = _normalise_layout_text(" ".join(str(token.get("text", "")) for token in ordered))
        if not candidate:
            continue
        similarity = SequenceMatcher(None, target, candidate).ratio()
        if best is None or similarity > best[0]:
            best = (similarity, page, ordered)
    if best is None or best[0] < 0.55:
        return None, None
    _, page, matches = best
    return page, [
        min(float(token["bbox"][0]) for token in matches),
        min(float(token["bbox"][1]) for token in matches),
        max(float(token["bbox"][2]) for token in matches),
        max(float(token["bbox"][3]) for token in matches),
    ]


def _bbox_for_value(value: str | None, tokens: list[dict[str, Any]]) -> tuple[int | None, list[float] | None]:
    if not value:
        return None, None
    needle = re.sub(r"\W", "", value).lower()
    matches = []
    for token in tokens:
        box = token.get("bbox")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        candidate = re.sub(r"\W", "", str(token.get("text", ""))).lower()
        if needle and (needle in candidate or candidate in needle):
            matches.append(token)
    if not matches:
        return None, None
    page = matches[0].get("page")
    same_page = [token for token in matches if token.get("page") == page]
    return page, [
        min(float(token["bbox"][0]) for token in same_page), min(float(token["bbox"][1]) for token in same_page),
        max(float(token["bbox"][2]) for token in same_page), max(float(token["bbox"][3]) for token in same_page),
    ]


def _field(value: Any, source_value: str | None, tokens: list[dict[str, Any]], *, confidence: float, method: str, status: str = "valid") -> dict[str, Any]:
    evidence_text = source_value or (str(value) if value is not None else None)
    page, bbox = _bbox_for_value(evidence_text, tokens)
    score = float(confidence)
    basis = "deterministic_label_and_source_text_heuristic; not a trained model probability"
    if value is not None and bbox is not None:
        matched_tokens = [
            token for token in tokens
            if token.get("page") == page and token.get("bbox")
            and token["bbox"][0] <= bbox[2] and token["bbox"][2] >= bbox[0]
            and token["bbox"][1] <= bbox[3] and token["bbox"][3] >= bbox[1]
        ]
        ocr_scores = [float(token["confidence"]) for token in matched_tokens if token.get("confidence_source") == "tesseract_word_confidence"]
        if ocr_scores:
            score = min(score, sum(ocr_scores) / len(ocr_scores))
            basis = "deterministic parser evidence capped by mean Tesseract confidence over the source bounding box; not a calibrated field probability"
    return {
        "value": value.isoformat() if isinstance(value, (date, datetime)) else (str(value) if isinstance(value, Decimal) else value),
        "normalized_value": value.isoformat() if isinstance(value, (date, datetime)) else (str(value) if isinstance(value, Decimal) else value),
        "confidence": round(score, 3) if value is not None else 0.0,
        "source_page": page,
        "bbox": bbox,
        "validation_status": status if value is not None else "missing",
        "extraction_method": method,
        "confidence_basis": basis,
    }


def extract_structured_fields(text: str, tokens: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rule-first extraction baseline; confidence describes evidence quality, not model probability."""
    text = text.replace("\u00a0", " ")
    vendor, vendor_line = _line_field(text, [r"(?:vendor|supplier|from)\s*(?:name)?\s*[:#-]\s*(.+)$"])
    if not vendor:
        candidates = [line.strip() for line in text.splitlines() if line.strip()]
        excluded = ("invoice", "bill to", "ship to", "purchase order", "date", "subtotal", "tax", "total", "description")
        vendor_line = next((line for line in candidates if not line.lower().startswith(excluded) and len(line) >= 3), None)
        vendor = vendor_line
    invoice_no, invoice_line = _line_field(text, [
        r"(?:invoice\s*(?:number|no\.?|#|id))\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{1,})",
    ])
    po_number, po_line = _line_field(text, [
        r"(?:purchase\s*order(?:\s*(?:number|no\.?|#))?|po\s*(?:number|no\.?|#))\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{1,})",
    ])
    tax_id, tax_line = _line_field(text, [r"(?:tax\s*(?:id|identifier|number)|vat\s*(?:id|number))\s*[:#-]?\s*([A-Z0-9-]{4,})"])
    issue_raw, issue_line = _line_field(text, [r"(?:invoice\s*date|date\s*issued|issue\s*date)\s*[:#-]?\s*([A-Za-z0-9, /-]{6,})"])
    due_raw, due_line = _line_field(text, [r"(?:due\s*date|payment\s*due)\s*[:#-]?\s*([A-Za-z0-9, /-]{6,})"])
    issue_date = _parse_date(issue_raw.split()[0] if issue_raw else "") or (_parse_date(issue_raw) if issue_raw else None)
    due_date = _parse_date(due_raw.split()[0] if due_raw else "") or (_parse_date(due_raw) if due_raw else None)

    currency = "USD"
    currency_match = re.search(r"\b(USD|EUR|GBP|INR|CAD|AUD)\b", text, re.IGNORECASE)
    if currency_match:
        currency = currency_match.group(1).upper()
    elif "€" in text:
        currency = "EUR"
    elif "£" in text:
        currency = "GBP"
    elif "₹" in text:
        currency = "INR"

    def money_for(labels: list[str], *, last: bool = False) -> tuple[Decimal | None, str | None]:
        pattern = re.compile(r"(?:" + "|".join(labels) + r")\s*[:#-]?\s*(?:USD\s*)?[$€£₹]?\s*\(?-?[\d,]+(?:\.\d{1,4})?\)?", re.IGNORECASE)
        matches = list(pattern.finditer(text))
        if not matches:
            return None, None
        match = matches[-1] if last else matches[0]
        amount_text = re.search(r"\(?-?[\d,]+(?:\.\d{1,4})?\)?$", match.group(0).strip())
        return (_normalise_amount(amount_text.group(0)), match.group(0)) if amount_text else (None, match.group(0))

    subtotal, subtotal_line = money_for([r"subtotal", r"sub-total"])
    tax_amount, tax_line = money_for([r"tax(?:\s+amount)?", r"vat(?:\s+amount)?"])
    shipping, shipping_line = money_for([r"shipping", r"freight"])
    discount, discount_line = money_for([r"discount"])
    total, total_line = money_for([r"grand\s+total", r"invoice\s+total", r"total\s+due", r"amount\s+due", r"total"], last=True)

    line_items: list[dict[str, Any]] = []
    line_pattern = re.compile(
        r"^\s*(?:(?P<sku>[A-Z0-9][A-Z0-9._/-]{1,})\s+)?(?P<description>[A-Za-z][A-Za-z0-9 &()/.,_-]{1,}?)\s+"
        r"(?P<qty>\d+(?:\.\d{1,4})?)\s+\$?(?P<unit>[\d,]+\.\d{2,4})\s+\$?(?P<amount>[\d,]+\.\d{2})\s*$",
        re.IGNORECASE,
    )
    in_table = False
    line_arithmetic_errors: list[dict[str, Any]] = []
    for line in text.splitlines():
        lowered = line.lower()
        if ("description" in lowered or "item" in lowered) and ("qty" in lowered or "quantity" in lowered) and ("unit" in lowered or "price" in lowered):
            in_table = True
            continue
        if in_table and any(word in lowered for word in ("subtotal", "tax:", "grand total", "invoice total", "amount due")):
            in_table = False
        match = line_pattern.match(line)
        if not match:
            # Explicit pipe-delimited tabular rows: SKU | description | quantity | unit | line total.
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 5 and re.fullmatch(r"\d+(?:\.\d+)?", parts[-3]):
                candidate = {"sku": parts[0] or None, "description": parts[1], "qty": parts[-3], "unit": parts[-2].replace("$", ""), "amount": parts[-1].replace("$", "")}
            else:
                continue
        else:
            candidate = match.groupdict()
        quantity = _safe_decimal(candidate["qty"])
        unit_price = _safe_decimal(candidate["unit"].replace(",", "").replace("$", ""))
        line_total = _normalise_amount(candidate["amount"])
        if quantity is None or unit_price is None or line_total is None or not candidate["description"].strip():
            continue
        source_line = line.strip()
        source_page, source_bbox = _bbox_for_source_line(source_line, tokens)
        expected_line_total = (quantity * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        arithmetic_valid = abs(expected_line_total - line_total) <= Decimal("0.05")
        row_confidence = 0.88 if in_table else 0.72
        if source_bbox is not None:
            row_tokens = [
                token for token in tokens
                if token.get("page") == source_page and token.get("bbox")
                and token["bbox"][0] <= source_bbox[2] and token["bbox"][2] >= source_bbox[0]
                and token["bbox"][1] <= source_bbox[3] and token["bbox"][3] >= source_bbox[1]
            ]
            ocr_scores = [float(token["confidence"]) for token in row_tokens if token.get("confidence_source") == "tesseract_word_confidence"]
            if ocr_scores:
                row_confidence = min(row_confidence, sum(ocr_scores) / len(ocr_scores))
        if not arithmetic_valid:
            line_arithmetic_errors.append({
                "code": "line_item_arithmetic", "field": "line_items",
                "line_number": len(line_items) + 1,
                "message": f"Line {len(line_items) + 1}: quantity × unit price is {expected_line_total}, but the extracted line total is {line_total}.",
            })
        line_items.append({
            "line_number": len(line_items) + 1,
            "sku": (candidate.get("sku") or "").strip() or None,
            "description": candidate["description"].strip(),
            "quantity": str(quantity), "unit_price": str(unit_price),
            "tax_rate": "0", "tax_amount": None, "line_total": str(line_total),
            "confidence": round(float(row_confidence), 3),
            "confidence_basis": "spatial row pattern and arithmetic evidence; OCR rows are capped by mean word confidence; not calibrated accuracy",
            "validation_status": "arithmetic_valid" if arithmetic_valid else "arithmetic_mismatch",
            "extraction_method": "spatial_table_row_pattern",
            "source_page": source_page,
            "bbox": source_bbox,
            "source_line": source_line,
        })

    fields: dict[str, Any] = {
        "vendor_name": _field(vendor, vendor_line, tokens, confidence=0.92 if vendor_line and vendor_line.lower().startswith(("vendor", "supplier", "from")) else 0.62, method="label_rule_or_first_non_header_line"),
        "vendor_tax_id": _field(tax_id, tax_line, tokens, confidence=0.94, method="labeled_tax_identifier"),
        "invoice_number": _field(invoice_no, invoice_line, tokens, confidence=0.95, method="labeled_invoice_identifier"),
        "purchase_order_number": _field(po_number, po_line, tokens, confidence=0.94, method="labeled_purchase_order_identifier"),
        "issue_date": _field(issue_date, issue_line, tokens, confidence=0.9, method="labeled_date_parse"),
        "due_date": _field(due_date, due_line, tokens, confidence=0.9, method="labeled_date_parse"),
        "currency": _field(currency, currency_match.group(0) if currency_match else None, tokens, confidence=0.94 if currency_match else 0.7, method="currency_code_or_symbol"),
        "subtotal": _field(subtotal, subtotal_line, tokens, confidence=0.93, method="labeled_money_parse"),
        "tax_amount": _field(tax_amount, tax_line, tokens, confidence=0.93, method="labeled_money_parse"),
        "shipping_amount": _field(shipping or Decimal("0.00"), shipping_line, tokens, confidence=0.85 if shipping is not None else 0.55, method="labeled_money_parse_or_zero_default"),
        "discount_amount": _field(discount or Decimal("0.00"), discount_line, tokens, confidence=0.85 if discount is not None else 0.55, method="labeled_money_parse_or_zero_default"),
        "total_amount": _field(total, total_line, tokens, confidence=0.94, method="labeled_money_parse"),
        "line_items": line_items,
    }
    errors: list[dict[str, Any]] = list(line_arithmetic_errors)
    for required in ("vendor_name", "invoice_number", "issue_date", "total_amount"): 
        if fields[required]["value"] is None:
            errors.append({"code": f"missing_{required}", "field": required, "message": f"Required field {required.replace('_', ' ')} was not extracted."})
    if not line_items:
        errors.append({"code": "no_line_items", "field": "line_items", "message": "No line items were parsed; verify the document table manually."})
    if subtotal is not None and line_items:
        line_sum = sum((_safe_decimal(item["line_total"]) or Decimal("0")) for item in line_items)
        if abs(line_sum - subtotal) > Decimal("0.05"):
            errors.append({"code": "line_subtotal_difference", "field": "subtotal", "message": f"Parsed line total {line_sum} differs from stated subtotal {subtotal}."})
    if all(value is not None for value in (subtotal, tax_amount, total)):
        calculated = subtotal + tax_amount + (shipping or Decimal("0")) - (discount or Decimal("0"))
        if abs(calculated - total) > Decimal("0.05"):
            errors.append({"code": "invoice_total_arithmetic", "field": "total_amount", "message": f"Subtotal + tax + shipping - discount = {calculated}, but invoice total is {total}."})
    return fields, errors


def _safe_decimal(value: str | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def process_document(data: bytes, filename: str, mime_type: str) -> DocumentExtraction:
    raw_text, tokens, page_count, engine_name = read_document(data, filename, mime_type)
    fields, errors = extract_structured_fields(raw_text, tokens)
    engine_version = f"{engine_name}; parser=spatial-rules-v2"
    if settings.layout_model_path:
        try:
            threshold = float(settings.layout_model_min_confidence)
            if not 0.0 <= threshold <= 1.0:
                raise ValueError("LAYOUT_MODEL_MIN_CONFIDENCE must be between 0 and 1")
            predictions, model_version = classify_tokens_with_local_model(
                tokens, settings.layout_model_path, device=settings.layout_model_device,
            )
            candidates = extract_entities(tokens, predictions)
            numeric_fields = {"subtotal", "tax_amount", "shipping_amount", "discount_amount", "total_amount"}
            for field_name, candidate in candidates.items():
                if float(candidate.get("confidence", 0)) < threshold:
                    continue
                normalized = candidate.get("normalized_value")
                if field_name in numeric_fields and _safe_decimal(normalized) is None:
                    errors.append({"code": "layout_model_candidate_invalid", "field": field_name, "message": "The configured layout model produced a non-numeric amount; the deterministic extraction was retained."})
                    continue
                if field_name in {"issue_date", "due_date"}:
                    try:
                        date.fromisoformat(str(normalized))
                    except (TypeError, ValueError):
                        errors.append({"code": "layout_model_candidate_invalid", "field": field_name, "message": "The configured layout model produced an ambiguous or invalid date; the deterministic extraction was retained."})
                        continue
                if field_name == "currency" and not re.fullmatch(r"[A-Z]{3}", str(normalized or "")):
                    errors.append({"code": "layout_model_candidate_invalid", "field": field_name, "message": "The configured layout model did not produce a three-letter currency code; the deterministic extraction was retained."})
                    continue
                deterministic = fields.get(field_name)
                candidate["deterministic_fallback"] = {
                    "value": deterministic.get("value") if deterministic else None,
                    "normalized_value": deterministic.get("normalized_value") if deterministic else None,
                    "extraction_method": deterministic.get("extraction_method", "spatial_rules") if deterministic else "spatial_rules",
                }
                fields[field_name] = candidate
                errors.append({
                    "code": "layout_model_human_validation_required", "field": field_name,
                    "message": "A locally configured layout-model candidate was used; verify it against the source document before relying on matching or risk results.",
                })
            engine_name = f"{engine_name}+local-token-classifier"
            engine_version = f"{engine_version}; local_token_classifier_config_sha256={model_version}"
        except (LayoutModelUnavailable, ValueError) as exc:
            # Preserve the deterministic path and make the fallback visible; no remote model
            # call is attempted and no document is sent outside this service.
            errors.append({"code": "layout_model_unavailable", "field": "document", "message": str(exc)})
            engine_version = f"{engine_version}; local_token_classifier=unavailable"
    return DocumentExtraction(
        raw_text=raw_text,
        tokens=tokens,
        fields=fields,
        validation_errors=errors,
        engine_name=engine_name,
        engine_version=engine_version,
        page_count=page_count,
    )
