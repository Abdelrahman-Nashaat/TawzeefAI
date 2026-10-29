from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.document_text import DocumentTextError, extract_document_text


def pdf_bytes(text=None, password=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text is not None:
        font = DictionaryObject({
            NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Helvetica'),
        })
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})
        })
        content = DecodedStreamObject()
        content.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def docx_bytes():
    document = Document()
    document.add_paragraph('Synthetic candidate: Python developer')
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = 'Skill'
    table.cell(0, 1).text = 'SQL'
    result = BytesIO()
    document.save(result)
    return result.getvalue()


def test_extracts_text_pdf_and_docx_including_tables():
    assert 'Python developer' in extract_document_text('resume.PDF', pdf_bytes('Python developer'))
    extracted = extract_document_text('resume.docx', docx_bytes())
    assert 'Synthetic candidate: Python developer' in extracted
    assert 'Skill' in extracted and 'SQL' in extracted


@pytest.mark.parametrize(('filename', 'content', 'code'), [
    ('resume.txt', b'hello', 'unsupported_type'),
    ('resume.pdf', b'', 'empty_file'),
    ('resume.pdf', b'bad PDF', 'invalid_document'),
    ('resume.docx', b'bad DOCX', 'invalid_document'),
    ('scanned.pdf', pdf_bytes(), 'no_text'),
    ('encrypted.pdf', pdf_bytes('secret', password='example'), 'protected_document'),
])
def test_reports_expected_failure(filename, content, code):
    with pytest.raises(DocumentTextError) as error:
        extract_document_text(filename, content)
    assert error.value.code == code


def test_rejects_large_input_before_parsing():
    with pytest.raises(DocumentTextError) as error:
        extract_document_text('resume.pdf', b'x' * 101, max_bytes=100)
    assert error.value.code == 'file_too_large'


def test_rejects_excessive_expanded_docx_before_parsing():
    data = BytesIO()
    with ZipFile(data, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', 'x' * 1000)
    with pytest.raises(DocumentTextError) as error:
        extract_document_text('resume.docx', data.getvalue(), max_expanded_bytes=500)
    assert error.value.code == 'document_too_large'


def test_enforces_text_and_page_limits():
    with pytest.raises(DocumentTextError) as error:
        extract_document_text('resume.pdf', pdf_bytes('abcd'), max_chars=3)
    assert error.value.code == 'document_too_large'
    assert extract_document_text('resume.pdf', pdf_bytes('abcd'), max_chars=4) == 'abcd'

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    data = BytesIO()
    writer.write(data)
    with pytest.raises(DocumentTextError) as error:
        extract_document_text('resume.pdf', data.getvalue(), max_pages=1)
    assert error.value.code == 'document_too_large'


def test_unsupported_office_compound_file_fails_cleanly():
    with pytest.raises(DocumentTextError) as error:
        extract_document_text('resume.docx', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')
    assert error.value.code == 'protected_document'


def test_empty_docx_reports_no_text():
    document = Document()
    data = BytesIO()
    document.save(data)
    with pytest.raises(DocumentTextError) as error:
        extract_document_text('blank.docx', data.getvalue())
    assert error.value.code == 'no_text'
