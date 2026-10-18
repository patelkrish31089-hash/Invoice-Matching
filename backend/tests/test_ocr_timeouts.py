from __future__ import annotations

import numpy as np
import pytest

from app.services import document_processing


def test_tesseract_call_uses_timeout_and_maps_process_timeout(monkeypatch):
    calls = []
    image = np.zeros((80, 120), dtype=np.uint8)

    monkeypatch.setattr(document_processing, "tesseract_available", lambda: True)
    monkeypatch.setattr(
        document_processing,
        "preprocess_image",
        lambda _image: (image, {"width": 120, "height": 80}),
    )
    monkeypatch.setattr(document_processing.cv2, "fastNlMeansDenoising", lambda source, *_args, **_kwargs: source)

    def timeout(*_args, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(document_processing.pytesseract, "image_to_data", timeout)

    with pytest.raises(document_processing.OCRTimeoutError, match="per-attempt OCR time limit"):
        document_processing._ocr_image(image, 1)

    assert len(calls) == 1
    assert calls[0] == document_processing.settings.ocr_timeout_seconds


def test_document_deadline_expires_before_starting_another_ocr_pass():
    with pytest.raises(document_processing.OCRTimeoutError, match="per-document"):
        document_processing._ocr_call_timeout(0.0)
