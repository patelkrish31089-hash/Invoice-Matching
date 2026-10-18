from pathlib import Path

import pytest

from app.services.layout_model import (
    LayoutModelUnavailable,
    TokenLabel,
    classify_tokens_with_local_model,
    extract_entities,
    normalize_bbox_to_layoutlm,
    normalize_entity_value,
)


def _token(text: str, bbox: list[float], page: int = 1) -> dict:
    return {"text": text, "bbox": bbox, "page": page, "page_size": [1000, 500]}


def test_layoutlm_boxes_are_normalized_clipped_and_ordered():
    assert normalize_bbox_to_layoutlm(_token("Widget", [20, 40, 240, 60])) == [20, 80, 240, 120]
    assert normalize_bbox_to_layoutlm(_token("offpage", [-5, 600, 1100, 700])) == [0, 1000, 1000, 1000]
    assert normalize_bbox_to_layoutlm({"text": "missing"}) == [0, 0, 0, 0]


def test_model_spans_merge_source_tokens_and_keep_uncalibrated_evidence():
    tokens = [
        _token("Northstar", [10, 10, 90, 30]),
        _token("Supplies", [95, 10, 160, 30]),
        _token("1,234.50", [800, 400, 920, 430]),
    ]
    predictions = [
        TokenLabel("B-VENDOR_NAME", 0.94),
        TokenLabel("I-VENDOR_NAME", 0.86),
        TokenLabel("S-TOTAL_AMOUNT", 0.99),
    ]
    entities = extract_entities(tokens, predictions)
    vendor = entities["vendor_name"]
    assert vendor["value"] == "Northstar Supplies"
    assert vendor["confidence"] == 0.9
    assert vendor["bbox"] == [10.0, 10.0, 160.0, 30.0]
    assert vendor["source_page"] == 1
    assert vendor["confidence_source"] == "layout_model_softmax_uncalibrated"
    assert entities["total_amount"]["normalized_value"] == "1234.50"
    assert entities["total_amount"]["validation_status"] == "model_candidate_unvalidated"


def test_model_entity_normalization_does_not_guess_ambiguous_dates():
    assert normalize_entity_value("currency", "usd") == "USD"
    assert normalize_entity_value("total_amount", "USD 1,234.50") == "1234.50"
    assert normalize_entity_value("issue_date", "2026-10-01") == "2026-10-01"
    assert normalize_entity_value("issue_date", "03/04/26") == "03/04/26"


def test_model_predictions_must_align_one_to_one_with_source_tokens():
    with pytest.raises(ValueError, match="exactly one"):
        extract_entities([_token("Total", [1, 1, 2, 2])], [])


def test_local_inference_fails_closed_when_checkpoint_is_not_configured(tmp_path: Path):
    with pytest.raises(LayoutModelUnavailable, match="config.json"):
        classify_tokens_with_local_model(
            [_token("Total", [1, 1, 2, 2])], tmp_path / "no-model",
        )


def test_document_processing_uses_local_candidate_but_forces_human_validation(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import document_processing

    source_token = _token("15.00", [800, 400, 920, 430])
    monkeypatch.setattr(settings, "layout_model_path", str(tmp_path / "checkpoint"))
    monkeypatch.setattr(settings, "layout_model_min_confidence", 0.75)
    monkeypatch.setattr(document_processing, "read_document", lambda *_args: ("Total 18.00", [source_token], 1, "synthetic-pdf-text"))
    monkeypatch.setattr(document_processing, "extract_structured_fields", lambda *_args: ({
        "total_amount": {"value": "18.00", "normalized_value": "18.00", "confidence": 0.8, "extraction_method": "spatial_rules"},
    }, []))
    monkeypatch.setattr(document_processing, "classify_tokens_with_local_model", lambda *_args, **_kwargs: ([TokenLabel("S-TOTAL_AMOUNT", 0.92)], "config-hash"))
    monkeypatch.setattr(document_processing, "extract_entities", lambda *_args: {
        "total_amount": {
            "value": "15.00", "normalized_value": "15.00", "confidence": 0.92,
            "validation_status": "model_candidate_unvalidated", "extraction_method": "layout_token_classification",
            "source_page": 1, "bbox": [800, 400, 920, 430],
        },
    })

    result = document_processing.process_document(b"synthetic", "invoice.pdf", "application/pdf")
    assert result.fields["total_amount"]["normalized_value"] == "15.00"
    assert result.fields["total_amount"]["deterministic_fallback"]["normalized_value"] == "18.00"
    assert any(error["code"] == "layout_model_human_validation_required" for error in result.validation_errors)
    assert "local_token_classifier_config_sha256=config-hash" in result.engine_version


def test_configured_but_unavailable_layout_model_preserves_deterministic_extraction(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import document_processing

    fallback = {"total_amount": {"value": "18.00", "normalized_value": "18.00", "confidence": 0.8, "extraction_method": "spatial_rules"}}
    monkeypatch.setattr(settings, "layout_model_path", str(tmp_path / "missing-checkpoint"))
    monkeypatch.setattr(document_processing, "read_document", lambda *_args: ("Total 18.00", [_token("18.00", [1, 1, 2, 2])], 1, "synthetic-pdf-text"))
    monkeypatch.setattr(document_processing, "extract_structured_fields", lambda *_args: (fallback.copy(), []))
    monkeypatch.setattr(document_processing, "classify_tokens_with_local_model", lambda *_args, **_kwargs: (_ for _ in ()).throw(LayoutModelUnavailable("checkpoint unavailable")))

    result = document_processing.process_document(b"synthetic", "invoice.pdf", "application/pdf")
    assert result.fields["total_amount"]["normalized_value"] == "18.00"
    assert any(error["code"] == "layout_model_unavailable" for error in result.validation_errors)
    assert result.engine_name == "synthetic-pdf-text"
