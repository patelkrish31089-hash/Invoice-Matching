from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np

from app.services import duplicate_embeddings, fraud


def test_embedding_loader_uses_a_local_checkpoint_and_forbids_remote_code_or_downloads(monkeypatch, tmp_path):
    model_path = tmp_path / "approved-local-model"
    model_path.mkdir()
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))

    duplicate_embeddings._load_model_from_local_path.cache_clear()
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeSentenceTransformer))
    try:
        model = duplicate_embeddings._load_model_from_local_path(str(model_path))
        assert isinstance(model, FakeSentenceTransformer)
        assert calls[0][1]["local_files_only"] is True
        assert calls[0][1]["trust_remote_code"] is False
    finally:
        duplicate_embeddings._load_model_from_local_path.cache_clear()


def test_local_embedding_scores_use_only_configured_local_model_and_cosine(monkeypatch):
    calls = []

    class FakeLocalModel:
        def encode(self, texts, **kwargs):
            calls.append((texts, kwargs))
            return np.asarray([[1.0, 0.0], [0.8, 0.6]])

    monkeypatch.setattr(duplicate_embeddings, "_load_model_from_local_path", lambda path: FakeLocalModel())
    current = {"invoice_number": "INV-1", "currency": "USD", "line_items": [{"description": "Custom valve"}]}
    past = {"invoice_number": "INV-9", "currency": "USD", "line_items": [{"description": "Custom valve"}]}

    scores = duplicate_embeddings.semantic_similarity_scores(current, [past], model_path="/approved/model")

    assert scores == [0.8]
    assert len(calls) == 1
    assert calls[0][1]["convert_to_numpy"] is True
    assert calls[0][1]["show_progress_bar"] is False


def test_local_embedding_absence_or_failure_degrades_to_deterministic_path(monkeypatch, tmp_path):
    current = {"invoice_number": "INV-1", "line_items": [{"description": "Widget"}]}
    past = {"invoice_number": "INV-2", "line_items": [{"description": "Widget"}]}
    assert duplicate_embeddings.semantic_similarity_scores(current, [past], model_path=None) == [None]
    assert duplicate_embeddings.semantic_similarity_scores(current, [past], model_path=str(tmp_path / "missing-model")) == [None]

    class BrokenModel:
        def encode(self, *_args, **_kwargs):
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(duplicate_embeddings, "_load_model_from_local_path", lambda _path: BrokenModel())
    assert duplicate_embeddings.semantic_similarity_scores(current, [past], model_path="/approved/model") == [None]


def test_duplicate_signal_can_use_local_embedding_candidate_without_claiming_probability(monkeypatch):
    monkeypatch.setattr(fraud.settings, "duplicate_embedding_model_path", "/approved/local/model")
    monkeypatch.setattr(fraud.settings, "duplicate_embedding_similarity_threshold", 0.92)
    monkeypatch.setattr(fraud, "semantic_similarity_scores", lambda *_args, **_kwargs: [0.96])
    current = {
        "id": "new", "vendor_id": "vendor-1", "invoice_number": "AB12",
        "total_amount": "50.00", "issue_date": "2026-05-01",
        "line_items": [{"sku": "A", "description": "Custom valve", "quantity": "2", "unit_price": "25", "line_total": "50"}],
    }
    past = {
        "id": "old", "vendor_id": "vendor-1", "invoice_number": "ZZ98",
        "total_amount": "50.00", "issue_date": "2026-05-10",
        "line_items": [{"sku": "B", "description": "Other material", "quantity": "2", "unit_price": "25", "line_total": "50"}],
    }

    results = fraud.detect_duplicates(current, [past])

    assert len(results) == 1
    assert results[0]["type"] == "near_duplicate"
    assert results[0]["evidence"]["embedding_cosine_similarity"] == 0.96
    assert "investigation candidate" in results[0]["explanation"]
