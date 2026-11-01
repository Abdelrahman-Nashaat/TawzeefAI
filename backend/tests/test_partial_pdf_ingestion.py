"""Mixed PDFs must not silently lose image-only or outlined-text pages."""

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.document_text import DocumentTextError, extract_document_text, extract_pdf_pages
from app.main import create_app


def make_pdf(kinds):
    writer = PdfWriter()
    for kind in kinds:
        page = writer.add_blank_page(width=100, height=100)
        if kind == 'blank':
            continue
        stream = DecodedStreamObject()
        if kind == 'text':
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream.set_data(b'BT /F1 12 Tf 10 50 Td (Synthetic Python text) Tj ET')
        elif kind == 'image':
            image = DecodedStreamObject()
            image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                          NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                          NameObject('/ColorSpace'): NameObject('/DeviceGray'),
                          NameObject('/BitsPerComponent'): NumberObject(8)})
            image.set_data(b'\x00')
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/XObject'): DictionaryObject({NameObject('/Im1'): writer._add_object(image)})})
            stream.set_data(b'q 100 0 0 100 0 0 cm /Im1 Do Q')
        elif kind == 'vector':
            stream.set_data(b'10 10 m 90 90 l S')
        else:
            raise ValueError(kind)
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize('graphic', ['image', 'vector'])
@pytest.mark.parametrize('extract', [extract_document_text, extract_pdf_pages])
def test_mixed_graphic_and_text_pages_are_not_silently_discarded(graphic, extract):
    with pytest.raises(DocumentTextError) as error:
        extract('sample.pdf', make_pdf(['text', graphic]))
    assert error.value.code == 'partial_text'


def test_blank_pages_keep_real_page_numbers():
    pages = extract_pdf_pages('sample.pdf', make_pdf(['text', 'blank', 'text']))
    assert [page['page'] for page in pages] == [1, 2, 3]
    assert pages[1]['text'] == ''
    assert extract_document_text('sample.pdf', make_pdf(['text', 'blank', 'text'])).count('Python') == 2


def test_mixed_pdf_uploads_fail_without_storing_partial_data(tmp_path):
    app = create_app(tmp_path / 'partial.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        response = client.post('/api/applications', params={'job_id': 'job-backend-python', 'filename': 'resume.pdf'},
                               content=make_pdf(['text', 'image']))
        assert response.status_code == 422 and response.json()['detail']['code'] == 'partial_text'
        assert client.get('/api/applications').json() == []
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        response = client.post('/api/knowledge/documents',
                               params={'title': 'Synthetic policy', 'audience': 'staff', 'filename': 'policy.pdf'},
                               content=make_pdf(['text', 'vector']))
        assert response.status_code == 422 and response.json()['detail']['code'] == 'partial_text'
        assert client.get('/api/knowledge/documents').json() == []
