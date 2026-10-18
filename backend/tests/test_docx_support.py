from __future__ import annotations

from email.message import EmailMessage
import hashlib
import hmac
import io
from pathlib import Path
import time
import zipfile

import pytest
from PIL import Image
from fastapi import HTTPException

from app.api.email_ingestion import _validate_email_document
from app.api.invoices import _verify_document
import app.services.docx_processing as docx_processing
from app.services.docx_processing import (
    DOCX_MIME_TYPE,
    DocxLimitError,
    DocxProcessingError,
    render_docx_text_preview,
    validate_docx,
)
from app.services.document_processing import process_document, read_document, render_document_page


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    b'</Types>'
)


def _paragraph(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _invoice_docx(*, extra_parts: dict[str, bytes] | None = None, document_xml: bytes | None = None) -> bytes:
    if document_xml is None:
        body = "".join([
            _paragraph("Vendor: Acme Systems LLC"),
            _paragraph("Tax ID: 12-3456789"),
            _paragraph("Invoice Number: INV-DOCX-100"),
            _paragraph("Invoice Date: 2026-10-08"),
            _paragraph("PO Number: PO-DOCX-100"),
            "<w:tbl><w:tr>"
            "<w:tc>" + _paragraph("Description") + "</w:tc>"
            "<w:tc>" + _paragraph("Qty") + "</w:tc>"
            "<w:tc>" + _paragraph("Unit Price") + "</w:tc>"
            "<w:tc>" + _paragraph("Amount") + "</w:tc>"
            "</w:tr><w:tr>"
            "<w:tc>" + _paragraph("SKU-1") + "</w:tc>"
            "<w:tc>" + _paragraph("Widget Gear") + "</w:tc>"
            "<w:tc>" + _paragraph("2") + "</w:tc>"
            "<w:tc>" + _paragraph("12.50") + "</w:tc>"
            "<w:tc>" + _paragraph("25.00") + "</w:tc>"
            "</w:tr></w:tbl>",
            _paragraph("Subtotal: 25.00"),
            _paragraph("Tax: 0.00"),
            _paragraph("Grand Total: 25.00 USD"),
        ])
        document_xml = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}">'
            f"<w:body>{body}</w:body></w:document>"
        ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", document_xml)
        for name, content in (extra_parts or {}).items():
            archive.writestr(name, content)
    return output.getvalue()


def test_docx_ooxml_text_and_tables_are_extracted_without_inventing_visual_boxes():
    payload = _invoice_docx()
    text, tokens, page_count, engine = read_document(
        payload, "synthetic-invoice.docx", DOCX_MIME_TYPE,
    )
    extraction = process_document(payload, "synthetic-invoice.docx", DOCX_MIME_TYPE)

    assert page_count == 1
    assert engine == "docx-ooxml-text"
    assert "Vendor: Acme Systems LLC" in text
    assert "SKU-1 | Widget Gear | 2 | 12.50 | 25.00" in text
    assert tokens and all(token["source"] == "docx_ooxml_text" for token in tokens)
    assert all(token["page"] is None and token["bbox"] is None for token in tokens)
    assert extraction.fields["vendor_name"]["value"] == "Acme Systems LLC"
    assert extraction.fields["invoice_number"]["value"] == "INV-DOCX-100"
    assert len(extraction.fields["line_items"]) == 1
    assert extraction.fields["line_items"][0]["description"] == "Widget Gear"
    assert extraction.fields["line_items"][0]["bbox"] is None
    assert extraction.fields["line_items"][0]["source_page"] is None


def test_docx_text_preview_is_valid_png_and_explicitly_reflowed():
    payload = _invoice_docx()
    preview = render_docx_text_preview(payload)
    image = Image.open(io.BytesIO(preview))
    assert image.format == "PNG"
    assert image.size == (1200, 1600)
    assert render_document_page(payload, "synthetic-invoice.docx", DOCX_MIME_TYPE, 1) == preview
    with pytest.raises(IndexError, match="text preview page"):
        render_document_page(payload, "synthetic-invoice.docx", DOCX_MIME_TYPE, 2)


def test_docx_portal_and_email_validation_share_the_bounded_package_reader():
    payload = _invoice_docx()
    assert _verify_document(payload, "invoice.docx") == (DOCX_MIME_TYPE, ".docx")
    assert _validate_email_document(payload, "invoice.docx") == (DOCX_MIME_TYPE, ".docx")
    validate_docx(payload)


def test_authenticated_invoice_upload_persists_docx_with_openxml_mime(api, monkeypatch):
    from app.config import settings
    from app.models import DocumentFile, Invoice

    monkeypatch.setattr(settings, "task_mode", "celery")
    monkeypatch.setattr("app.api.invoices.dispatch_outbox_event", lambda *args, **kwargs: None)
    response = api["client"].post(
        "/api/v1/invoices/upload",
        headers=api["admin"],
        files={"file": ("synthetic-invoice.docx", _invoice_docx(), DOCX_MIME_TYPE)},
    )
    assert response.status_code == 202, response.text
    with api["app"].state.session_factory() as db:
        invoice = db.get(Invoice, response.json()["invoice_id"])
        document = db.query(DocumentFile).filter_by(invoice_id=response.json()["invoice_id"]).one()
        assert invoice is not None and invoice.source == "upload"
        assert document.original_filename == "synthetic-invoice.docx"
        assert document.mime_type == DOCX_MIME_TYPE
        assert Path(document.stored_path).is_file()


def test_invalid_docx_package_is_rejected_by_internal_upload_validation():
    with pytest.raises(HTTPException) as error:
        _verify_document(b"not a zip archive", "invoice.docx")
    assert error.value.status_code == 415


def test_docx_path_traversal_and_macro_parts_are_rejected():
    traversal = _invoice_docx(extra_parts={"../outside.txt": b"synthetic"})
    with pytest.raises(DocxProcessingError, match="unsafe package path"):
        validate_docx(traversal)

    macro = _invoice_docx(extra_parts={"word/vbaProject.bin": b"synthetic macro bytes"})
    with pytest.raises(DocxProcessingError, match="Macro-enabled"):
        validate_docx(macro)


def test_docx_rejects_xml_entities_and_overcompressed_parts():
    entity_xml = (
        f'<!DOCTYPE w:document [<!ENTITY x "expanded">]>'
        f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>'
    ).encode("utf-8")
    with pytest.raises(DocxProcessingError, match="prohibited XML"):
        validate_docx(_invoice_docx(document_xml=entity_xml))

    compressed_bomb = _invoice_docx(extra_parts={"custom/large.xml": b"A" * 500_000})
    with pytest.raises(DocxLimitError, match="compressed"):
        validate_docx(compressed_bomb)
    with pytest.raises(HTTPException) as error:
        _verify_document(compressed_bomb, "invoice.docx")
    assert error.value.status_code == 413


def test_docx_body_xml_metadata_limit_is_enforced_before_xml_parsing(monkeypatch):
    monkeypatch.setattr(docx_processing, "MAX_DOCX_DOCUMENT_XML_BYTES", 128)
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" * 8
    xml = (
        f'<w:document xmlns:w="{W_NS}"><w:body>{_paragraph(text)}'
        "</w:body></w:document>"
    ).encode("utf-8")
    with pytest.raises(DocxLimitError, match="body exceeds"):
        validate_docx(_invoice_docx(document_xml=xml))


def test_signed_email_ingestion_persists_docx_attachment_through_existing_pipeline(api, monkeypatch):
    from app import config
    from app.api import email_ingestion
    from app.models import DocumentFile, EmailIngestionEvent, Invoice, ProcessingJob, ProcessingOutbox, UploadReceipt
    from app.services.malware_scan import MalwareScanResult

    secret = "test-only-docx-email-secret-0123456789"
    event_id = "synthetic-docx-email-event"
    monkeypatch.setattr(config.settings, "email_ingest_webhook_secret", secret)
    monkeypatch.setattr(config.settings, "email_ingest_allowed_senders", "ap@example.test")
    monkeypatch.setattr(config.settings, "email_ingest_max_message_mb", 25)
    monkeypatch.setattr(config.settings, "email_ingest_signature_tolerance_seconds", 300)
    monkeypatch.setattr(config.settings, "task_mode", "celery")
    monkeypatch.setattr(
        email_ingestion, "scan_submission",
        lambda _payload: MalwareScanResult(status="not_configured"),
    )
    dispatches = []
    monkeypatch.setattr(
        email_ingestion, "dispatch_outbox_event",
        lambda *_args, **_kwargs: dispatches.append(True) or None,
    )

    payload = _invoice_docx()
    message = EmailMessage()
    message["From"] = "AP Team <ap@example.test>"
    message["To"] = "invoices@example.test"
    message["Subject"] = "Synthetic DOCX invoice"
    message["Message-ID"] = "<synthetic-docx-event@example.test>"
    message.set_content("Please process this synthetic Word invoice.")
    message.add_attachment(
        payload,
        maintype="application",
        subtype=DOCX_MIME_TYPE.split("/", 1)[1],
        filename="synthetic-invoice.docx",
    )
    raw = message.as_bytes()
    timestamp = str(int(time.time()))
    signed_bytes = timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + raw
    signature = hmac.new(secret.encode(), signed_bytes, hashlib.sha256).hexdigest()

    response = api["client"].post(
        "/api/v1/ingestion/email",
        content=raw,
        headers={
            "Content-Type": "message/rfc822",
            "X-InvoiceGuard-Event-ID": event_id,
            "X-InvoiceGuard-Timestamp": timestamp,
            "X-InvoiceGuard-Signature": f"sha256={signature}",
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["queued_attachments"] == 1
    assert body["payment_execution"] is False
    assert dispatches == [True]

    with api["app"].state.session_factory() as db:
        event = db.query(EmailIngestionEvent).filter_by(provider_event_id=event_id).one()
        invoice = db.get(Invoice, body["items"][0]["invoice_id"])
        document = db.query(DocumentFile).filter_by(invoice_id=invoice.id).one()
        receipt = db.query(UploadReceipt).filter_by(invoice_id=invoice.id).one()
        job = db.query(ProcessingJob).filter_by(invoice_id=invoice.id).one()
        outbox = db.query(ProcessingOutbox).filter_by(invoice_id=invoice.id).one()
        assert event.state == "completed"
        assert invoice.source == "email"
        assert document.original_filename == "synthetic-invoice.docx"
        assert document.mime_type == DOCX_MIME_TYPE
        assert Path(document.stored_path).read_bytes() == payload
        assert receipt.state == "committed" and receipt.source == "email"
        assert job.state == "queued" and outbox.event_type == "process_invoice"
