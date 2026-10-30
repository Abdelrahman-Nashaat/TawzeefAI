"""Bounded extraction of text from PDF and DOCX files.

This module does not perform OCR. It treats file contents as untrusted data and
does not run them as instructions. The caller must enforce authorization and
limit request bodies before buffering uploads; this function receives bytes that
are already in memory. The returned text may contain personal information.
"""

import re
from io import BytesIO
from pathlib import PurePath
from zipfile import ZipFile

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_DOCX_MEMBERS = 1000
MAX_PAGES = 100
MAX_TEXT_CHARS = 200_000


class DocumentTextError(ValueError):
    """Safe-to-display error with a stable, machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def extract_pdf_pages(filename: str, content: bytes) -> list[dict]:
    """Extract numbered PDF text pages, including blank pages, without OCR."""
    if not isinstance(filename, str) or not isinstance(content, bytes):
        raise TypeError('filename must be str and content must be bytes.')
    if PurePath(filename).suffix.lower() != '.pdf':
        raise DocumentTextError('unsupported_type', 'Only PDF documents are supported.')
    if not content:
        raise DocumentTextError('empty_file', 'The document is empty.')
    if len(content) > MAX_FILE_BYTES:
        raise DocumentTextError('file_too_large', 'The uploaded file exceeds the limit.')
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise DocumentTextError('protected_document', 'Password-protected PDFs are unsupported.')
        if len(reader.pages) > MAX_PAGES:
            raise DocumentTextError('document_too_large', 'The PDF has too many pages.')
        pages = []
        total = 0
        for number, page in enumerate(reader.pages, 1):
            content_text = (page.extract_text() or '').strip()
            if '\x00' in content_text:
                raise DocumentTextError('invalid_document', 'PDF text contains invalid characters.')
            total += len(content_text) + (1 if pages and content_text else 0)
            if total > MAX_TEXT_CHARS:
                raise DocumentTextError('document_too_large', 'Extracted text exceeds the limit.')
            pages.append({'page': number, 'text': content_text})
    except DocumentTextError:
        raise
    except Exception as exc:
        raise DocumentTextError('invalid_document', 'The document is invalid or could not be read.') from exc
    if not any(page['text'] for page in pages):
        raise DocumentTextError('no_text', 'No text could be extracted; OCR may be required.')
    return pages


def _append_text(parts: list[str], text: str, length: int, max_chars: int) -> int:
    text = text.strip()
    if not text:
        return length
    length += len(text) + (1 if parts else 0)
    if length > max_chars:
        raise DocumentTextError('document_too_large', 'Extracted text exceeds the limit.')
    parts.append(text)
    return length


def _table_text(table: Table):
    # Cell XML can be shared by merged cells; avoid repeating those cells.
    for row in table.rows:
        seen = set()
        for cell in row.cells:
            if cell._tc in seen:
                continue
            seen.add(cell._tc)
            for item in cell.iter_inner_content():
                if isinstance(item, Paragraph):
                    yield item.text
                else:
                    yield from _table_text(item)


def extract_document_text(
    filename: str,
    content: bytes,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    max_expanded_bytes: int = MAX_EXPANDED_BYTES,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    """Extract text or raise DocumentTextError; extension must be PDF or DOCX.

    Error codes: unsupported_type, empty_file, file_too_large,
    document_too_large, protected_document, invalid_document, and no_text.
    ``no_text`` includes image-only PDFs, which need a separate OCR pipeline.
    PDF extraction supports text layers, not images. DOCX paragraphs and table
    cells are read in document order; headers, footers, and images are omitted.
    Parameters are limits and must be positive integers.
    """
    if not all(isinstance(value, int) and value > 0 for value in (
        max_bytes, max_expanded_bytes, max_pages, max_chars
    )):
        raise ValueError('All extraction limits must be positive integers.')
    if not isinstance(filename, str) or not isinstance(content, bytes):
        raise TypeError('filename must be str and content must be bytes.')
    extension = PurePath(filename).suffix.lower()
    if extension not in {'.pdf', '.docx'}:
        raise DocumentTextError('unsupported_type', 'Only PDF and DOCX documents are supported.')
    if not content:
        raise DocumentTextError('empty_file', 'The document is empty.')
    if len(content) > max_bytes:
        raise DocumentTextError('file_too_large', 'The uploaded file exceeds the limit.')

    parts: list[str] = []
    length = 0
    try:
        if extension == '.pdf':
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise DocumentTextError('protected_document', 'Password-protected PDFs are unsupported.')
            if len(reader.pages) > max_pages:
                raise DocumentTextError('document_too_large', 'The PDF has too many pages.')
            for page in reader.pages:
                length = _append_text(parts, page.extract_text() or '', length, max_chars)
        else:
            if content.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
                raise DocumentTextError(
                    'protected_document', 'Encrypted or legacy Office files are unsupported.'
                )
            with ZipFile(BytesIO(content)) as archive:
                members = archive.infolist()
                if len(members) > MAX_DOCX_MEMBERS or sum(m.file_size for m in members) > max_expanded_bytes:
                    raise DocumentTextError('document_too_large', 'The expanded DOCX exceeds the limit.')
                if any(m.flag_bits & 1 for m in members):
                    raise DocumentTextError('protected_document', 'Password-protected DOCX files are unsupported.')
            document = Document(BytesIO(content))
            for item in document.iter_inner_content():
                texts = [item.text] if isinstance(item, Paragraph) else _table_text(item)
                for text in texts:
                    length = _append_text(parts, text, length, max_chars)
    except DocumentTextError:
        raise
    except Exception as exc:
        # Parser details can expose filenames or document content; keep them internal.
        raise DocumentTextError('invalid_document', 'The document is invalid or could not be read.') from exc
    if not parts:
        raise DocumentTextError('no_text', 'No text could be extracted; OCR may be required.')
    return '\n'.join(parts)


def extract_docx_sections(filename: str, content: bytes) -> tuple[str, list[dict] | None]:
    """Extract bounded text and sections marked by Word's built-in heading styles.

    DOCX has no reliable physical page numbers. Unstyled documents retain the
    original chunking behavior. Sections are untrusted document content.
    """
    if PurePath(filename).suffix.lower() != '.docx':
        raise DocumentTextError('unsupported_type', 'Only DOCX documents are supported.')
    text = extract_document_text(filename, content)  # Performs all size and ZIP checks first.
    try:
        document = Document(BytesIO(content))
        sections: list[dict] = []
        parts: list[str] = []
        heading = None
        found = False
        for item in document.iter_inner_content():
            if isinstance(item, Paragraph):
                value = item.text.strip()
                style_id = item.style.style_id if item.style else ''
                is_heading = bool(value and len(value) <= 200 and re.fullmatch(r'Heading[1-9]', style_id))
                if is_heading:
                    if parts:
                        sections.append({'section': heading, 'text': '\n'.join(parts)})
                        if len(sections) >= 1000:
                            raise DocumentTextError('document_too_large', 'The document has too many sections.')
                    heading, parts, found = value, [], True
                if value:
                    parts.append(value)
            else:
                parts.extend(value.strip() for value in _table_text(item) if value.strip())
        if parts:
            sections.append({'section': heading, 'text': '\n'.join(parts)})
        if len(sections) > 1000 or '\n'.join(s['text'] for s in sections) != text:
            raise DocumentTextError('invalid_document', 'Document sections could not be safely extracted.')
    except DocumentTextError:
        raise
    except Exception as exc:
        raise DocumentTextError('invalid_document', 'The document is invalid or could not be read.') from exc
    return text, sections if found else None
