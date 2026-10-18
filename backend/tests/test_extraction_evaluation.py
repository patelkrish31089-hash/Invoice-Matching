from __future__ import annotations

import pytest

from app.services.extraction_evaluation import evaluate_extractions


def test_extraction_evaluation_reports_normalized_fields_numeric_error_lines_and_confidence():
    records = [
        {
            "id": "synthetic-1",
            "gold": {
                "vendor_name": "Acme Parts",
                "invoice_number": "INV-100",
                "issue_date": "2026-01-02",
                "total_amount": "100.00",
                "line_items": [{
                    "sku": "A-1", "description": "Widget Pro", "quantity": "2",
                    "unit_price": "50.00", "line_total": "100.00",
                }],
            },
            "prediction": {
                "vendor_name": {"value": " acme parts ", "confidence": 0.9},
                "invoice_number": {"value": "inv 100", "confidence": 0.8},
                "issue_date": {"value": "2026-01-02", "confidence": 0.9},
                "total_amount": {"value": "100.00", "confidence": 0.95},
                "line_items": [{
                    "sku": "A-1", "description": "Widget Pro", "quantity": "2.0",
                    "unit_price": "50", "line_total": "100.00",
                }],
            },
        },
        {
            "id": "synthetic-2",
            "gold": {
                "vendor_name": "Other Supply",
                "invoice_number": "INV-200",
                "total_amount": "200.00",
                "line_items": [{
                    "sku": "B-2", "description": "Cable Bundle", "quantity": "4",
                    "unit_price": "25.00", "line_total": "100.00",
                }],
            },
            "prediction": {
                "vendor_name": {"value": "Other Supply", "confidence": 0.7},
                "invoice_number": {"value": "INV-201", "confidence": 0.7},
                "total_amount": {"value": "201.00", "confidence": 0.7},
                "line_items": [{
                    "sku": "B-2", "description": "Cable Bundle", "quantity": "4",
                    "unit_price": "25.00", "line_total": "100.00",
                }],
            },
        },
    ]

    report = evaluate_extractions(records)
    invoice_number = report["field_metrics"]["invoice_number"]
    total = report["field_metrics"]["total_amount"]
    lines = report["line_item_metrics"]

    assert report["documents"] == 2
    assert invoice_number["true_positive"] == 1
    assert invoice_number["false_positive"] == 1
    assert invoice_number["false_negative"] == 1
    assert invoice_number["precision"] == 0.5
    assert invoice_number["recall"] == 0.5
    assert invoice_number["normalized_match_accuracy_all_documents"] == 0.5
    assert total["numeric_mae"] == 0.5
    assert total["numeric_pairs"] == 2
    assert lines["gold_rows"] == 2
    assert lines["predicted_rows"] == 2
    assert lines["matched_rows"] == 2
    assert lines["row_match_f1"] == 1.0
    assert lines["exact_line_set_rate"] == 1.0
    assert invoice_number["confidence_diagnostics"]["sample_size"] == 2
    assert "not recalibrated" in report["confidence_note"]


def test_invalid_numeric_strings_do_not_become_false_normalized_matches():
    report = evaluate_extractions([{
        "gold": {"total_amount": "not-money"},
        "prediction": {"total_amount": {"value": "different", "confidence": 0.5}},
    }])

    metrics = report["field_metrics"]["total_amount"]
    assert metrics["true_positive"] == 0
    assert metrics["false_positive"] == 1
    assert metrics["false_negative"] == 1
    assert metrics["numeric_pairs"] == 0


def test_extraction_evaluation_requires_labeled_records_and_valid_shapes():
    with pytest.raises(ValueError, match="At least one labeled"):
        evaluate_extractions([])
    with pytest.raises(ValueError, match="gold.*prediction"):
        evaluate_extractions([{"id": "bad", "gold": []}])
    with pytest.raises(ValueError, match="line_items must be a list"):
        evaluate_extractions([{"id": "bad-lines", "gold": {"line_items": "no"}, "prediction": {}}])
