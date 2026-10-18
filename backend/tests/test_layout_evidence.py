from __future__ import annotations

import io

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services.document_processing import (
    extract_structured_fields,
    read_document,
    tesseract_available,
)


def _layout_tokens(text: str) -> list[dict]:
    tokens = []
    for row_index, line in enumerate(text.splitlines()):
        y = 20 + row_index * 24
        for word_index, word in enumerate(line.split()):
            x = 20 + word_index * 74
            tokens.append({
                "text": word,
                "page": 1,
                "bbox": [x, y, x + 66, y + 18],
                "page_size": [800, 500],
                "layout_group": f"pdf:1:0:{row_index}",
                "word_order": word_index,
                "confidence": 1.0,
                "confidence_source": "embedded_pdf_text_not_ocr",
                "source": "pdf_text_layer",
            })
    return tokens


def test_extracted_line_items_keep_row_bbox_and_flag_line_arithmetic():
    text = "\n".join([
        "Vendor: Example Components",
        "Invoice Number: INV-100",
        "Description Qty Unit Price Amount",
        "SKU-1 Widget XL 2 12.50 25.00",
        "SKU-2 Cable Pack 3 10.00 29.00",
        "Subtotal: 54.00",
        "Tax: 0.00",
        "Grand Total: 54.00",
    ])

    fields, errors = extract_structured_fields(text, _layout_tokens(text))
    lines = fields["line_items"]

    assert len(lines) == 2
    assert lines[0]["source_page"] == 1
    assert lines[0]["bbox"] == [20, 92, 456, 110]
    assert lines[0]["extraction_method"] == "spatial_table_row_pattern"
    assert lines[0]["validation_status"] == "arithmetic_valid"
    assert lines[1]["validation_status"] == "arithmetic_mismatch"
    assert any(item["code"] == "line_item_arithmetic" and item.get("line_number") == 2 for item in errors)


def test_scanned_synthetic_image_uses_local_tesseract_and_emits_word_boxes():
    if not tesseract_available():
        pytest.skip("System Tesseract is not installed in this test environment")
    image = Image.new("RGB", (1400, 600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=36)
    draw.text((60, 60), "Vendor: Example Supply", fill="black", font=font)
    draw.text((60, 150), "Invoice Number: TEST-100", fill="black", font=font)
    draw.text((60, 240), "Grand Total: 125.00 USD", fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")

    text, tokens, page_count, engine = read_document(output.getvalue(), "synthetic-scan.png", "image/png")

    assert page_count == 1
    assert "tesseract-ocr" in engine
    assert text.strip()
    assert tokens
    assert all(token["source"] == "tesseract_ocr" and len(token["bbox"]) == 4 for token in tokens)
    assert all(token.get("layout_group") for token in tokens)


def test_multipage_pdf_with_repeated_table_headers_retains_both_line_sources():
    document = fitz.open()
    first = document.new_page(width=612, height=792)
    first.insert_text((48, 50), "Vendor: Example Components")
    first.insert_text((48, 70), "Invoice Number: INV-MULTI-01")
    first.insert_text((48, 100), "Description Qty Unit Price Amount")
    first.insert_text((48, 120), "SKU-1 Widget XL 2 12.50 25.00")
    second = document.new_page(width=612, height=792)
    second.insert_text((48, 50), "Description Qty Unit Price Amount")
    second.insert_text((48, 70), "SKU-2 Cable Pack 3 10.00 30.00")
    second.insert_text((48, 100), "Subtotal: 55.00")
    second.insert_text((48, 120), "Tax: 0.00")
    second.insert_text((48, 140), "Grand Total: 55.00")
    payload = document.tobytes()
    document.close()

    text, tokens, page_count, engine = read_document(payload, "synthetic-multipage.pdf", "application/pdf")
    fields, errors = extract_structured_fields(text, tokens)

    assert page_count == 2
    assert "pymupdf-text" in engine
    assert len(fields["line_items"]) == 2
    assert [line["source_page"] for line in fields["line_items"]] == [1, 2]
    assert all(line["bbox"] and line["bbox"][2] > line["bbox"][0] for line in fields["line_items"])
    assert not any(error["code"] == "line_subtotal_difference" for error in errors)
