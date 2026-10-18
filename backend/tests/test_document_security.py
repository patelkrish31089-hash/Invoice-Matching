from __future__ import annotations

import struct
import zlib

import pymupdf as fitz
import pytest

from app.services.document_processing import (
    MAX_DOCUMENT_PAGES,
    MAX_IMAGE_PIXELS,
    read_document,
    render_document_page,
)


def test_pdf_page_count_is_bounded_before_document_processing():
    document = fitz.open()
    for _ in range(MAX_DOCUMENT_PAGES + 1):
        document.new_page()
    payload = document.tobytes()
    document.close()

    with pytest.raises(ValueError, match="page processing limit"):
        read_document(payload, "synthetic-many-pages.pdf", "application/pdf")


def test_pdf_raster_page_dimensions_are_bounded_before_rendering():
    document = fitz.open()
    document.new_page(width=4000, height=4000)
    payload = document.tobytes()
    document.close()

    with pytest.raises(ValueError, match="raster-processing limit"):
        read_document(payload, "synthetic-oversized-page.pdf", "application/pdf")
    with pytest.raises(ValueError, match="raster-preview limit"):
        render_document_page(payload, "synthetic-oversized-page.pdf", "application/pdf", 1)


def _png_header(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, content: bytes) -> bytes:
        body = kind + content
        return struct.pack(">I", len(content)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # A small compressed scanline is sufficient for lazy header inspection; the decoder is never invoked.
    idat = zlib.compress(b"\x00" + b"\x00" * (width * 3), level=1)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_image_pixel_limit_is_checked_before_opencv_decode():
    oversized = _png_header(8_000, 6_000)
    assert 8_000 * 6_000 > MAX_IMAGE_PIXELS

    with pytest.raises(ValueError, match="pixel processing limit"):
        read_document(oversized, "synthetic-huge-image.png", "image/png")
    with pytest.raises(ValueError, match="pixel processing limit"):
        render_document_page(oversized, "synthetic-huge-image.png", "image/png", 1)
