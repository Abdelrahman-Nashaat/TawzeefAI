"""Reviewer-selected PDF headings retain exact page and source provenance."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.db import connect
from app.knowledge import _document_passages
from app.knowledge_documents import fetch_authorized_documents
from app.main import create_app


def make_pdf():
    writer = PdfWriter()
    for lines in [('Overview', 'Annual leave is fourteen days.', 'Overview', 'Employees qualify after six months.'),
                  ('Eligibility continues here.', 'Overview', 'Use approved devices.')]:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 50 700 Td ' + ' 0 -18 Td '.join(f'({line}) Tj' for line in lines) + ' ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': role + '-demo-only'}).status_code == 200


def test_repeated_headings_are_selected_by_page_and_line_and_rechecked(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    app = create_app(tmp_path / 'selected.sqlite3', seed_demo=True)
    pdf = make_pdf()
    with TestClient(app) as client:
        login(client, 'candidate')
        preview_path = '/api/knowledge/pdf-section-preview'
        assert client.post(preview_path, params={'filename': 'policy.pdf'}, content=pdf).status_code == 403
        login(client, 'hr')
        preview = client.post(preview_path, params={'filename': 'policy.pdf'}, content=pdf)
        assert preview.status_code == 200
        body = preview.json()
        assert body['pages'][0]['text'].splitlines()[0] == 'Overview'
        assert client.get('/api/knowledge/documents').json() == []
        marks = [{'page': 1, 'line': 0}, {'page': 1, 'line': 2}, {'page': 2, 'line': 1}]
        params = {'title': 'Synthetic policy', 'audience': 'staff', 'filename': 'policy.pdf',
                  'collection': 'handbook', 'pdf_selected_headings': json.dumps(marks),
                  'pdf_preview_sha256': body['source_sha256']}
        response = client.post('/api/knowledge/documents', params=params, content=pdf)
        assert response.status_code == 201 and response.json()['pdf_selected_heading_count'] == 3
        document = fetch_authorized_documents(app.state.database_path, 'employee')[0]
        passages = _document_passages(document)
        assert [(item.page, item.section) for item in passages] == [
            (1, 'Overview'), (1, 'Overview'), (2, 'Overview'), (2, 'Overview'),
        ]
        assert 'fourteen days' in passages[0].text and 'six months' not in passages[0].text
        assert 'six months' in passages[1].text and 'continues' in passages[2].text
        assert 'approved devices' in passages[3].text
        login(client, 'employee')
        chat = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        message = client.post(f'/api/conversations/{chat}/messages', json={'question': 'approved devices'}).json()
        assert message['sources'][0]['page'] == 2 and message['sources'][0]['section'] == 'Overview'
        with connect(app.state.database_path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (response.json()['id'],))
        saved = client.get(f'/api/conversations/{chat}').json()['messages'][0]
        assert saved['mode'] == 'unavailable' and saved['sources'] == []
        with connect(app.state.database_path) as db:
            row = db.execute('SELECT pages_json FROM knowledge_documents WHERE id=?', (response.json()['id'],)).fetchone()
            pages = json.loads(row['pages_json'])
            pages[0]['selected_sections_v1'][0]['section'] = 'Forged'
            db.execute("UPDATE knowledge_documents SET audience='staff',pages_json=? WHERE id=?",
                       (json.dumps(pages), response.json()['id']))
        assert client.post('/api/knowledge/answer', json={'question': 'approved devices'}).json()['sources'] == []


def test_invalid_or_changed_preview_never_stores_a_document(tmp_path):
    app = create_app(tmp_path / 'invalid.sqlite3', seed_demo=True)
    pdf = make_pdf()
    with TestClient(app) as client:
        login(client, 'hr')
        preview = client.post('/api/knowledge/pdf-section-preview', params={'filename': 'policy.pdf'}, content=pdf).json()
        params = {'title': 'Synthetic policy', 'audience': 'staff', 'filename': 'policy.pdf',
                  'pdf_preview_sha256': preview['source_sha256']}
        for marks in ([], [{'page': 1, 'line': 0}, {'page': 1, 'line': 0}], [{'page': 2, 'line': 99}],
                      [{'page': True, 'line': 0}], [{'page': 1, 'line': -1}]):
            response = client.post('/api/knowledge/documents',
                                   params={**params, 'pdf_selected_headings': json.dumps(marks)}, content=pdf)
            assert response.status_code == 422
        mismatch = client.post('/api/knowledge/documents',
                               params={**params, 'pdf_selected_headings': '[{"page":1,"line":0}]'},
                               content=pdf + b'\n')
        assert mismatch.status_code == 409
        assert client.get('/api/knowledge/documents').json() == []


def test_ocr_provenance_with_selected_heading(tmp_path, monkeypatch):
    from app import knowledge_documents

    monkeypatch.setenv('TAWZEEF_ENABLE_OCR', '1')
    pages = [{'page': 1, 'text': 'Overview\nNative text.'},
             {'page': 2, 'text': 'Overview\nScanned text.'}]
    monkeypatch.setattr(knowledge_documents, 'extract_pdf_pages_with_ocr', lambda *_: (pages, [2]))
    app = create_app(tmp_path / 'ocr.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'hr')
        preview = client.post('/api/knowledge/pdf-section-preview',
                              params={'filename': 'policy.pdf', 'ocr': 'true'}, content=b'synthetic').json()
        response = client.post('/api/knowledge/documents', params={
            'title': 'Synthetic policy', 'audience': 'staff', 'filename': 'policy.pdf', 'ocr': 'true',
            'pdf_preview_sha256': preview['source_sha256'],
            'pdf_selected_headings': json.dumps([{'page': 1, 'line': 0}, {'page': 2, 'line': 0}]),
        }, content=b'synthetic')
        assert response.status_code == 201
        passages = _document_passages(fetch_authorized_documents(app.state.database_path, 'employee')[0])
        assert [(p.page, p.section, p.extraction) for p in passages] == [
            (1, 'Overview', 'native'), (2, 'Overview', 'ocr'),
        ]
        assert passages[1].extraction_notice


def test_access_revoked_during_preview_or_upload_blocks_return_and_storage(tmp_path, monkeypatch):
    from app import knowledge_documents

    pdf = make_pdf()
    app = create_app(tmp_path / 'revoked.sqlite3', seed_demo=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client, 'hr')
        initial = client.post('/api/knowledge/pdf-section-preview',
                              params={'filename': 'policy.pdf'}, content=pdf).json()
        original = knowledge_documents.extract_pdf_pages

        def revoke(filename, content):
            result = original(filename, content)
            with connect(app.state.database_path) as db:
                db.execute("UPDATE users SET active=0 WHERE username='hr'")
            return result

        monkeypatch.setattr(knowledge_documents, 'extract_pdf_pages', revoke)
        response = client.post('/api/knowledge/pdf-section-preview',
                               params={'filename': 'policy.pdf'}, content=pdf)
        assert response.status_code == 401 and 'pages' not in response.text
        with connect(app.state.database_path) as db:
            db.execute("UPDATE users SET active=1 WHERE username='hr'")
        login(client, 'hr')
        response = client.post('/api/knowledge/documents', params={
            'title': 'Synthetic policy', 'audience': 'staff', 'filename': 'policy.pdf',
            'pdf_preview_sha256': initial['source_sha256'],
            'pdf_selected_headings': '[{"page":1,"line":0}]',
        }, content=pdf)
        assert response.status_code == 401
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0] == 0


def test_preview_and_upload_share_bounded_extraction(tmp_path, monkeypatch):
    from app import knowledge_documents

    app = create_app(tmp_path / 'bounded.sqlite3', seed_demo=True)
    active = maximum = 0
    guard = threading.Lock()

    def slow_extract(_filename, _content):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.2)
            return [{'page': 1, 'text': 'Heading\nA policy.'}]
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(knowledge_documents, 'extract_pdf_pages', slow_extract)
    with TestClient(app) as client:
        login(client, 'hr')
        def call(index):
            if index % 2:
                return client.post('/api/knowledge/documents', params={
                    'title': f'Policy {index}', 'audience': 'staff', 'filename': 'policy.pdf',
                }, content=b'synthetic').status_code
            return client.post('/api/knowledge/pdf-section-preview',
                               params={'filename': 'policy.pdf'}, content=b'synthetic').status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(call, range(4)))
        assert statuses == [200, 201, 200, 201]
        assert maximum == 2
