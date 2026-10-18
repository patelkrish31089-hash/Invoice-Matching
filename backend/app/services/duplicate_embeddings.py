from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import numpy as np


def _invoice_text(invoice: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("invoice_number", "purchase_order_number", "currency"):
        value = invoice.get(key)
        if value:
            parts.append(f"{key.replace('_', ' ')}: {str(value).strip()}")
    rows = invoice.get("line_items")
    if isinstance(rows, list):
        for row in rows[:500]:
            if not isinstance(row, dict):
                continue
            values = [
                row.get("sku"), row.get("description"), row.get("quantity"),
                row.get("unit_price"), row.get("line_total"),
            ]
            text = " ".join(str(value).strip() for value in values if value not in (None, ""))
            if text:
                parts.append(f"line: {text}")
    return "\n".join(parts)[:100_000]


@lru_cache(maxsize=2)
def _load_model_from_local_path(model_path: str):
    """Load a caller-supplied model directory only; never resolve/download a hub model."""
    path = Path(model_path).expanduser()
    if not path.is_dir():
        return None
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(
            str(path.resolve()),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
    except Exception:
        return None


def semantic_similarity_scores(
    current: dict[str, Any],
    history: Iterable[dict[str, Any]],
    *,
    model_path: str | None,
) -> list[float | None]:
    """Return cosine similarities for an optional local model; safely fall back when absent.

    The configured path must contain an approved sentence-transformer model. No model
    download is attempted, and output scores are triage features, not calibrated duplicate
    probabilities. List positions correspond to the history iterable positions.
    """
    rows = list(history)
    if not rows or not model_path:
        return [None] * len(rows)
    model = _load_model_from_local_path(model_path)
    if model is None:
        return [None] * len(rows)
    texts = [_invoice_text(current), *(_invoice_text(row) for row in rows)]
    if not texts[0].strip():
        return [None] * len(rows)
    try:
        vectors = np.asarray(model.encode(
            texts,
            normalize_embeddings=False,
            convert_to_numpy=True,
            show_progress_bar=False,
        ), dtype=float)
        if vectors.ndim != 2 or vectors.shape[0] != len(texts) or vectors.shape[1] < 1 or not np.isfinite(vectors).all():
            return [None] * len(rows)
        current_vector = vectors[0]
        current_norm = float(np.linalg.norm(current_vector))
        if current_norm <= 0:
            return [None] * len(rows)
        output: list[float | None] = []
        for vector in vectors[1:]:
            norm = float(np.linalg.norm(vector))
            if norm <= 0:
                output.append(None)
                continue
            score = float(np.dot(current_vector, vector) / (current_norm * norm))
            if not np.isfinite(score):
                output.append(None)
            else:
                output.append(round(float(np.clip(score, -1.0, 1.0)), 6))
        return output
    except Exception:
        # Inference trouble must not prevent deterministic duplicate screening or invoice intake.
        return [None] * len(rows)
