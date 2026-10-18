from __future__ import annotations

import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageFont


DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_CONTAINER_BYTES = 32 * 1024 * 1024
MAX_DOCX_ENTRIES = 256
MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DOCX_PART_BYTES = 16 * 1024 * 1024
MAX_DOCX_DOCUMENT_XML_BYTES = 8 * 1024 * 1024
MAX_DOCX_TEXT_CHARACTERS = 2_000_000
MAX_DOCX_TOKENS = 100_000
MAX_DOCX_COMPRESSION_RATIO = 250
MAX_PREVIEW_CHARACTERS = 12_000

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_P = f"{{{_W_NS}}}p"
_W_TBL = f"{{{_W_NS}}}tbl"
_W_TR = f"{{{_W_NS}}}tr"
_W_TC = f"{{{_W_NS}}}tc"
_W_T = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BR = f"{{{_W_NS}}}br"
_W_CR = f"{{{_W_NS}}}cr"
_W_BODY = f"{{{_W_NS}}}body"


class DocxProcessingError(ValueError):
    """A DOCX package is malformed, unsafe, or unsupported."""


class DocxLimitError(DocxProcessingError):
    """A DOCX package exceeds a documented resource bound."""


def _validate_member_name(name: str) -> None:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise DocxProcessingError("The Word document contains an unsafe package path")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts[:-1]) or parts[-1] in {".", ".."}:
        raise DocxProcessingError("The Word document contains an unsafe package path")
    normalized = posixpath.normpath(name)
    if normalized == ".." or normalized.startswith("../"):
        raise DocxProcessingError("The Word document contains an unsafe package path")


def _read_part(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise DocxLimitError("A Word document package part exceeds the safe parsing limit")
    try:
        with archive.open(info, "r") as stream:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = stream.read(min(64 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise DocxLimitError("A Word document package part expands beyond the safe parsing limit")
                chunks.append(chunk)
    except DocxProcessingError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
        raise DocxProcessingError("The Word document package is malformed or unreadable") from exc
    return b"".join(chunks)


def _parse_xml(payload: bytes, description: str) -> ET.Element:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise DocxProcessingError(f"The Word document {description} contains a prohibited XML declaration")
    try:
        return ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise DocxProcessingError(f"The Word document {description} is malformed XML") from exc


def _open_document_xml(data: bytes) -> tuple[zipfile.ZipFile, ET.Element]:
    if not data:
        raise DocxProcessingError("The Word document is empty")
    if len(data) > MAX_DOCX_CONTAINER_BYTES:
        raise DocxLimitError("The Word document exceeds the safe package-size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise DocxProcessingError("The file is not a valid OpenXML Word document") from exc

    try:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_DOCX_ENTRIES:
            raise DocxLimitError("The Word document has an unsafe number of package entries")
        names: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            _validate_member_name(info.filename)
            if info.filename in names:
                raise DocxProcessingError("The Word document contains duplicate package paths")
            names.add(info.filename)
            if info.flag_bits & 0x1:
                raise DocxProcessingError("Encrypted Word document packages are not accepted")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise DocxProcessingError("The Word document uses an unsupported ZIP compression method")
            if info.file_size > MAX_DOCX_PART_BYTES:
                raise DocxLimitError("A Word document package part exceeds the safe parsing limit")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES:
                raise DocxLimitError("The Word document expands beyond the safe package-size limit")
            if info.file_size and info.compress_size == 0:
                raise DocxProcessingError("The Word document contains an invalid compressed package part")
            if info.compress_size and info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO:
                raise DocxLimitError("The Word document contains a suspiciously compressed package part")
            if "vbaproject.bin" in info.filename.casefold():
                raise DocxProcessingError("Macro-enabled Word documents are not accepted")

        required = {"[Content_Types].xml", "word/document.xml"}
        if not required.issubset(names):
            raise DocxProcessingError("The file is missing required OpenXML Word document parts")
        content_types_info = archive.getinfo("[Content_Types].xml")
        document_info = archive.getinfo("word/document.xml")
        if content_types_info.file_size > 1024 * 1024:
            raise DocxLimitError("The Word document content-type manifest exceeds the safe parsing limit")
        _parse_xml(_read_part(archive, content_types_info, 1024 * 1024), "content-type manifest")
        if document_info.file_size > MAX_DOCX_DOCUMENT_XML_BYTES:
            raise DocxLimitError("The Word document body exceeds the safe XML parsing limit")
        root = _parse_xml(_read_part(archive, document_info, MAX_DOCX_DOCUMENT_XML_BYTES), "body")
        if root.tag != f"{{{_W_NS}}}document":
            raise DocxProcessingError("The file does not contain a WordprocessingML document body")
        return archive, root
    except Exception:
        archive.close()
        raise


def _paragraph_text(paragraph: ET.Element) -> str:
    pieces: list[str] = []
    for node in paragraph.iter():
        if node.tag == _W_T:
            pieces.append(node.text or "")
        elif node.tag in {_W_TAB, _W_BR, _W_CR}:
            pieces.append(" ")
    return re.sub(r"[\t\r\n ]+", " ", "".join(pieces)).strip()


def _table_rows(table: ET.Element) -> list[str]:
    rows: list[str] = []
    for row in table.findall(_W_TR):
        cells: list[str] = []
        for cell in row.findall(_W_TC):
            cell_paragraphs = [_paragraph_text(paragraph) for paragraph in cell.iter(_W_P)]
            cells.append(" ".join(item for item in cell_paragraphs if item))
        rendered = " | ".join(cells).strip()
        if rendered.replace("|", "").strip():
            rows.append(rendered)
    return rows


def _document_blocks(parent: ET.Element):
    for child in list(parent):
        if child.tag == _W_P:
            text = _paragraph_text(child)
            if text:
                yield text
        elif child.tag == _W_TBL:
            yield from _table_rows(child)
        else:
            # Content controls/custom XML can wrap ordinary Word paragraphs. Recurse
            # through wrappers while table/paragraph branches above remain single-pass.
            yield from _document_blocks(child)


def _read_docx(data: bytes, *, include_tokens: bool) -> tuple[str, list[dict]]:
    archive, root = _open_document_xml(data)
    try:
        body = root.find(_W_BODY)
        if body is None:
            raise DocxProcessingError("The Word document has no main body")
        lines: list[str] = []
        character_count = 0
        for line in _document_blocks(body):
            character_count += len(line) + 1
            if character_count > MAX_DOCX_TEXT_CHARACTERS:
                raise DocxLimitError("The extracted Word document text exceeds the safe processing limit")
            lines.append(line)
        text = "\n".join(lines)
        if not text.strip():
            raise DocxProcessingError("The Word document contains no readable body text")
        if not include_tokens:
            return text, []

        tokens: list[dict] = []
        for block_index, line in enumerate(lines):
            for word_index, word in enumerate(re.findall(r"\S+", line)):
                if len(tokens) >= MAX_DOCX_TOKENS:
                    raise DocxLimitError("The Word document has too many text tokens to process safely")
                tokens.append({
                    "text": word,
                    "page": None,
                    "bbox": None,
                    "page_size": None,
                    "layout_group": f"docx:block:{block_index}",
                    "word_order": word_index,
                    "confidence": 1.0,
                    "confidence_source": "ooxml_text_extraction_not_visual_accuracy",
                    "source": "docx_ooxml_text",
                })
        return text, tokens
    finally:
        archive.close()


def validate_docx(data: bytes) -> None:
    """Validate bounded OOXML parts and extractability without retaining token data."""
    _read_docx(data, include_tokens=False)


def read_docx(data: bytes) -> tuple[str, list[dict]]:
    """Extract Word paragraphs and table rows; Word pagination/visual boxes are not inferred."""
    return _read_docx(data, include_tokens=True)


def _wrap_text(text: str, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    result: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            result.append(current)
            current = word
        else:
            current = candidate
    result.append(current)
    return result


def render_docx_text_preview(data: bytes) -> bytes:
    """Render a clearly labeled reflowed text preview, never a fabricated Word page image."""
    text, _ = _read_docx(data, include_tokens=False)
    image = Image.new("RGB", (1200, 1600), "#ffffff")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=26)
    body_font = ImageFont.load_default(size=20)
    note_font = ImageFont.load_default(size=16)
    draw.rectangle((0, 0, 1200, 132), fill="#eef3f8")
    draw.text((52, 28), "WORD DOCUMENT · TEXT-ONLY PREVIEW", fill="#17314d", font=title_font)
    draw.text((52, 72), "Reflowed text; original Word pagination, layout, and visual source boxes are not represented.", fill="#526579", font=note_font)

    preview_text = text[:MAX_PREVIEW_CHARACTERS]
    truncated = len(text) > len(preview_text)
    wrapped: list[str] = []
    for source_line in preview_text.splitlines():
        wrapped.extend(_wrap_text(source_line, draw, body_font, 1090))
    max_lines = 55
    visible_lines = wrapped[:max_lines]
    if len(wrapped) > max_lines:
        truncated = True
    y = 164
    line_height = 24
    for line in visible_lines:
        safe_line = line
        try:
            draw.text((54, y), safe_line, fill="#263746", font=body_font)
        except UnicodeEncodeError:
            draw.text((54, y), safe_line.encode("latin-1", "replace").decode("latin-1"), fill="#263746", font=body_font)
        y += line_height
    if truncated:
        draw.text((54, y + 8), "Preview truncated. Download the original Word document to inspect all text and formatting.", fill="#8a4b08", font=note_font)
    draw.rectangle((0, 1536, 1200, 1600), fill="#f6f8fb")
    draw.text((52, 1555), "Source evidence is extracted text only; no exact page number or coordinate is asserted.", fill="#526579", font=note_font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
