"""E1 — document bytes -> clean text, or a typed DocLoadError (never a silent
empty ingest: an image-only PDF needing OCR must be a visible gap).

The parser is chosen by extension, and checked against the bytes: a web page
served at a .pdf URL (a bot challenge, a landing page) is a web page, not an
"unreadable PDF"; a file whose URL carries no extension is read by what it is."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader


class DocLoadError(Exception):
    """The document could not be turned into text."""


class EmptyDocument(DocLoadError):
    """No usable text (image-only PDF, empty file). Also raised by store.ingest."""


class WebPage(DocLoadError):
    """The bytes are an HTML page, not a document."""


_MIN_CHARS = 20  # under this, extraction found no real text


def is_web_page(data: bytes) -> bool:
    """An HTML page by its first bytes (a BOM, whitespace or a comment may lead)."""
    head = bytes(data[:4096]).lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return True
    return head.startswith((b"<!--", b"<head", b"<meta", b"<?xml")) and b"<html" in head


def decode_text(data: bytes) -> tuple[str, str]:
    """(text, encoding) for bytes that are text in an unstated encoding: UTF-8 (a
    BOM is dropped) first, else the 8-bit code page whose non-ASCII characters read
    as letters — Windows-1252 and Mac Roman both decode almost anything, and a Mac
    export read as Windows-1252 turns every á into ‡."""
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    best = None
    for enc in ("cp1252", "mac_roman"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        high = [ch for ch in text if ord(ch) > 127]
        score = sum(1 if ch.isalpha() else -1 for ch in high)
        if best is None or score > best[0]:
            best = (score, text, enc)
    return best[1], best[2]                 # mac_roman decodes every byte


def _sniff(data: bytes, suffix: str) -> str | None:
    """The parser the bytes call for when the name gives none we read (a repository's
    `viewcontent.cgi`, a bare `5954`). Text only for a name with no extension at all:
    `image.png` is not a text file because its bytes happen to decode."""
    if data[:5] == b"%PDF-":
        return ".pdf"
    if data[:4] == b"PK\x03\x04" and b"word/" in data[:65536]:
        return ".docx"
    if suffix:
        return None
    try:
        text = data[:65536].decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return ".txt" if text and printable / len(text) > 0.95 else None


def extract_text(data: bytes, filename: str) -> str:
    if is_web_page(data):
        raise WebPage(f"{filename!r} is a web page (HTML), not a document — the link "
                      "returned a page instead of the file")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".txt", ".md", ".pdf", ".docx"):
        suffix = _sniff(data, suffix) or suffix
    if suffix in (".txt", ".md"):
        text = decode_text(data)[0]
    elif suffix == ".pdf":
        text = _pdf_text(data, filename)
    elif suffix == ".docx":
        text = _docx_text(data, filename)
    else:
        raise DocLoadError(
            f"Cannot extract text from {filename!r} — supported: .txt, .md, .pdf, .docx")
    text = text.lstrip("﻿")
    if len(text.strip()) < _MIN_CHARS:
        raise EmptyDocument(
            f"{filename!r} yielded no usable text — likely an image-only PDF "
            "(needs OCR) or an empty file")
    return text


def _pdf_text(data: bytes, filename: str) -> str:
    try:
        reader = PdfReader(BytesIO(data))
        return "\n\n".join(filter(None, (page.extract_text() for page in reader.pages)))
    except Exception as exc:  # pypdf raises a zoo of types on malformed files
        raise DocLoadError(f"unreadable PDF {filename!r}: {exc}") from exc


def _docx_text(data: bytes, filename: str) -> str:
    try:
        doc = Document(BytesIO(data))
    except Exception as exc:
        raise DocLoadError(f"unreadable DOCX {filename!r}: {exc}") from exc
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:  # bulletins put the numbers in tables
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)
