"""Bounded extraction of text from PDF and DOCX files.

It treats file contents as untrusted data and does not run them as instructions.
Optional OCR executes locally only when explicitly requested. The caller must enforce authorization and
limit request bodies before buffering uploads; this function receives bytes that
are already in memory. The returned text may contain personal information.
"""

import json
import os
import re
import signal
import subprocess
import sys
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
OCR_NOTICE = ('OCR is approximate. Review the document; text embedded in images on pages '
              'with a text layer may be missed.')


def require_ocr_enabled() -> None:
    if os.environ.get('TAWZEEF_ENABLE_OCR') != '1':
        raise DocumentTextError('ocr_disabled', 'Local OCR is disabled on this server.')


def _run_ocr_worker(command: list[str], content: bytes, timeout: float = 125) -> tuple[int, bytes]:
    """Run the local worker without application secrets and clean up its process tree."""
    allowed = ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TESSDATA_PREFIX', 'TAWZEEF_TESSERACT_PATH')
    env = {key: value for key, value in os.environ.items() if key.casefold() in {item.casefold() for item in allowed}}
    env['PYTHONIOENCODING'] = 'utf-8'
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
    try:
        output, _ = process.communicate(input=content, timeout=timeout)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        raise
    finally:
        if os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class DocumentTextError(ValueError):
    """Safe-to-display error with a stable, machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


_PDF_PAINT_OPERATORS = frozenset((
    b'Do', b'INLINE IMAGE', b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*', b'sh',
    b'Tj', b'TJ', b"'", b'"',
))


def _page_has_unextracted_content(page) -> bool:
    """Flag nonblank graphics or text operations when extraction yields no text.

    This intentionally does not classify visible content: a decorative image or
    line can trigger the same warning as a scanned page. Conversely, extracted
    text on a page does not establish that all of its visible content was read.
    """
    contents = page.get_contents()
    return contents is not None and any(operator in _PDF_PAINT_OPERATORS for _, operator in contents.operations)


def _check_partial_pdf(has_text: bool, has_unextracted_content: bool) -> None:
    if has_text and has_unextracted_content:
        raise DocumentTextError(
            'partial_text',
            'Some PDF pages contain graphics or unextractable text without a text layer. '
            'Review the document and provide a complete text-based PDF; OCR may be required.',
        )


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
        unextracted_content = False
        for number, page in enumerate(reader.pages, 1):
            content_text = (page.extract_text() or '').strip()
            if not content_text and _page_has_unextracted_content(page):
                unextracted_content = True
            if '\x00' in content_text:
                raise DocumentTextError('invalid_document', 'PDF text contains invalid characters.')
            total += len(content_text) + (1 if pages and content_text else 0)
            if total > MAX_TEXT_CHARS:
                raise DocumentTextError('document_too_large', 'Extracted text exceeds the limit.')
            pages.append({'page': number, 'text': content_text})
        _check_partial_pdf(any(page['text'] for page in pages), unextracted_content)
    except DocumentTextError:
        raise
    except Exception as exc:
        raise DocumentTextError('invalid_document', 'The document is invalid or could not be read.') from exc
    if not any(page['text'] for page in pages):
        raise DocumentTextError('no_text', 'No text could be extracted; OCR may be required.')
    return pages


def extract_pdf_pages_with_ocr(filename: str, content: bytes, language: str = 'eng') -> tuple[list[dict], list[int]]:
    """Extract PDF pages, using local OCR only on pages with no text layer.

    Explicit caller opt-in is required. OCR is approximate, bounded to five
    pages, and does not detect images on a page that already has extracted text.
    """
    if PurePath(filename).suffix.lower() != '.pdf':
        raise DocumentTextError('unsupported_type', 'OCR is available only for PDF documents.')
    if not content:
        raise DocumentTextError('empty_file', 'The document is empty.')
    if len(content) > MAX_FILE_BYTES:
        raise DocumentTextError('file_too_large', 'The uploaded file exceeds the limit.')
    if language not in ('eng', 'eng+ara'):
        raise DocumentTextError('invalid_ocr_language', 'Unsupported OCR language selection.')
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise DocumentTextError('protected_document', 'Password-protected PDFs are unsupported.')
        if len(reader.pages) > MAX_PAGES:
            raise DocumentTextError('document_too_large', 'The PDF has too many pages.')
        pages, indices = [], []
        for index, page in enumerate(reader.pages):
            text = (page.extract_text() or '').strip()
            if '\x00' in text:
                raise DocumentTextError('invalid_document', 'PDF text contains invalid characters.')
            pages.append({'page': index + 1, 'text': text})
            if not text and _page_has_unextracted_content(page):
                indices.append(index)
        if len(indices) > 5:
            raise DocumentTextError('ocr_limit', 'OCR supports at most five pages per upload.')
        if indices:
            returncode, output = _run_ocr_worker(
                [sys.executable, '-m', 'app.ocr_worker', language, ','.join(map(str, indices))], content)
            if returncode == 2:
                raise DocumentTextError('ocr_language_unavailable', 'The selected OCR language data is not installed.')
            if returncode:
                raise ValueError('OCR worker failed')
            if len(output) > 2_000_000:
                raise ValueError('OCR output too large')
            result = json.loads(output)
            if not isinstance(result, list) or len(result) != len(indices):
                raise ValueError('Invalid OCR output')
            for index, item in zip(indices, result, strict=True):
                if (not isinstance(item, dict) or item.get('index') != index or not isinstance(item.get('text'), str)
                        or not item['text'].strip() or '\x00' in item['text']):
                    raise ValueError('Invalid OCR output')
                pages[index]['text'] = item['text'].strip()
        if sum(len(page['text']) + 1 for page in pages) > MAX_TEXT_CHARS:
            raise DocumentTextError('document_too_large', 'Extracted text exceeds the limit.')
        if not any(page['text'] for page in pages):
            raise DocumentTextError('no_text', 'No text could be extracted.')
        return pages, [index + 1 for index in indices]
    except DocumentTextError:
        raise
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
        raise DocumentTextError('ocr_unavailable', 'Local OCR failed or the selected language is unavailable.') from None
    except Exception:  # noqa: BLE001 - malformed PDF parsers must not leak document data.
        raise DocumentTextError('invalid_document', 'The document is invalid or could not be read.') from None


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
    document_too_large, protected_document, invalid_document, no_text, and partial_text.
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
            unextracted_content = False
            for page in reader.pages:
                text = page.extract_text() or ''
                if not text.strip() and _page_has_unextracted_content(page):
                    unextracted_content = True
                length = _append_text(parts, text, length, max_chars)
            _check_partial_pdf(bool(parts), unextracted_content)
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
