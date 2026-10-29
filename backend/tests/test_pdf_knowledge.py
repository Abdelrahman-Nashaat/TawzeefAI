import hashlib
import json
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.conversations import _digest
from app.db import connect
from app.knowledge import Source
from app.main import create_app


def pdf_pages(contents, *, password=None):
    writer = PdfWriter()
    for text in contents:
        page = writer.add_blank_page(width=612, height=792)
        if text is not None:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def image_only_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    image = DecodedStreamObject()
    image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                  NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                  NameObject('/ColorSpace'): NameObject('/DeviceGray'), NameObject('/BitsPerComponent'): NumberObject(8)})
    image.set_data(b'\x00')
    page[NameObject('/Resources')] = DictionaryObject({
        NameObject('/XObject'): DictionaryObject({NameObject('/Im1'): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(b'q 100 0 0 100 0 0 cm /Im1 Do Q')
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_existing_conversation_source_digests_remain_compatible():
    old = {'id': 'db-doc-1-p1', 'title': 'Policy', 'text': 'Policy text'}
    digest = hashlib.sha256(json.dumps(old, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    assert _digest(Source(**old)) == digest


def test_pdf_pages_are_real_and_conversation_rechecks_permissions_across_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'pdf.sqlite3'
    document = pdf_pages(['Aurora equipment requires an approval.', None,
                          'Nebula sabbatical lasts eleven days.'])
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        upload = client.post('/api/knowledge/documents', params={
            'title': 'Handbook', 'audience': 'staff', 'filename': 'handbook.pdf',
        }, content=document)
        assert upload.status_code == 201
        assert upload.json()['page_count'] == 3 and upload.json()['pages_with_text'] == 2
        doc_id = upload.json()['id']
        login(client, 'candidate')
        assert client.post('/api/knowledge/answer', json={'question': 'nebula sabbatical'}).json()['sources'] == []
        login(client, 'employee')
        answer = client.post('/api/knowledge/answer', json={'question': 'nebula sabbatical'}).json()
        assert answer['sources'][0]['page'] == 3
        assert answer['sources'][0]['text'] == 'Nebula sabbatical lasts eleven days.'
        conversation = client.post('/api/conversations', json={}).json()['id']
        message = client.post(f'/api/conversations/{conversation}/messages', json={'question': 'nebula sabbatical'})
        assert message.status_code == 201 and message.json()['sources'][0]['page'] == 3
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'employee')
        saved = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert saved['sources'][0]['page'] == 3
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))
        assert client.post('/api/knowledge/answer', json={'question': 'nebula sabbatical'}).json()['sources'] == []
        saved = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert saved['mode'] == 'unavailable' and saved['sources'] == []
        login(client, 'hr')
        assert client.post('/api/knowledge/answer', json={'question': 'nebula sabbatical'}).json()['sources'][0]['page'] == 3
        # A stale page map must never resurrect content after the document changes.
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET text='Changed policy' WHERE id=?", (doc_id,))
        assert client.post('/api/knowledge/answer', json={'question': 'nebula sabbatical'}).json()['sources'] == []
        assert client.post(f'/api/knowledge/documents/{doc_id}/deactivate').status_code == 200
        assert client.post('/api/knowledge/answer', json={'question': 'nebula sabbatical'}).json()['sources'] == []


def test_unreadable_and_blank_pdf_rejected_without_persistence(tmp_path):
    with TestClient(create_app(tmp_path / 'pdf.sqlite3', seed_demo=True)) as client:
        login(client, 'hr')
        params = {'title': 'Handbook', 'audience': 'hr', 'filename': 'handbook.pdf'}
        for content, expected in [(b'bad pdf', 'invalid_document'), (pdf_pages([None]), 'no_text'),
                                  (image_only_pdf(), 'no_text'),
                                  (pdf_pages(['Text'], password='secret'), 'protected_document')]:
            result = client.post('/api/knowledge/documents', params=params, content=content)
            assert result.status_code == 422 and result.json()['detail']['code'] == expected
        assert client.get('/api/knowledge/documents').json() == []
