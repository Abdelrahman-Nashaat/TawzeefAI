"""Explicit PDF headings retain page identity without affecting legacy imports."""

import json
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.db import connect
from app.knowledge import _document_passages
from app.knowledge_documents import annotate_pdf_markdown_pages, fetch_authorized_documents
from app.main import create_app


def make_marked_pdf():
    writer = PdfWriter()
    for lines in [('# Leave', 'Annual leave is fourteen days.', '## Eligibility', 'Employees qualify after six months.'),
                  ('Eligibility continues on this page.', '# Equipment', 'Use approved devices.')]:
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


def test_explicit_markdown_sections_separate_same_page_and_carry_forward(tmp_path):
    app = create_app(tmp_path / 'marked.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        response = client.post('/api/knowledge/documents', params={
            'title': 'Synthetic handbook', 'audience': 'staff', 'filename': 'handbook.pdf',
            'collection': 'handbook', 'pdf_markdown_sections': 'true',
        }, content=make_marked_pdf())
        assert response.status_code == 201
        source = fetch_authorized_documents(app.state.database_path, 'employee')[0]
        passages = _document_passages(source)
        assert [(p.page, p.section) for p in passages] == [
            (1, 'Leave'), (1, 'Eligibility'), (2, 'Eligibility'), (2, 'Equipment'),
        ]
        assert 'Eligibility' not in passages[0].text
        assert 'six months' in passages[1].text
        assert 'Equipment' not in passages[2].text
        assert 'Use approved devices' in passages[3].text


def test_legacy_upload_unchanged_and_ambiguous_lines_are_not_headings(tmp_path):
    app = create_app(tmp_path / 'legacy.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        response = client.post('/api/knowledge/documents', params={
            'title': 'Legacy handbook', 'audience': 'staff', 'filename': 'handbook.pdf',
            'collection': 'handbook',
        }, content=make_marked_pdf())
        assert response.status_code == 201
        doc = fetch_authorized_documents(app.state.database_path, 'employee')[0]
        assert all('markdown_sections_v1' not in page for page in json.loads(doc['pages_json']))
        passages = _document_passages(doc)
        assert [(p.id, p.page, p.section) for p in passages] == [
            (f"db-doc-{response.json()['id']}-p1", 1, None),
            (f"db-doc-{response.json()['id']}-p2", 2, None),
        ]
        assert 'Leave' in passages[0].text and 'Eligibility' in passages[0].text
    annotated = annotate_pdf_markdown_pages([{'page': 1, 'text': 'UPPERCASE TITLE\n#NoSpace\nA rule.\n'}])
    assert annotated[0]['markdown_sections_v1'] == [{'start': 0, 'end': len(annotated[0]['text']), 'section': None}]


def test_sections_fail_closed_if_metadata_changes_and_old_conversation_rechecks_access(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    app = create_app(tmp_path / 'permissions.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('hr')
        response = client.post('/api/knowledge/documents', params={
            'title': 'Synthetic handbook', 'audience': 'staff', 'filename': 'handbook.pdf',
            'collection': 'handbook', 'pdf_markdown_sections': 'true',
        }, content=make_marked_pdf())
        document_id = response.json()['id']
        login('candidate')
        assert client.post('/api/knowledge/answer', json={'question': 'approved devices'}).json()['sources'] == []
        login('employee')
        chat = client.post('/api/conversations', json={}).json()['id']
        message = client.post(f'/api/conversations/{chat}/messages', json={'question': 'approved devices'}).json()
        assert message['sources'][0]['page'] == 2 and message['sources'][0]['section'] == 'Equipment'
        with connect(app.state.database_path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (document_id,))
        saved = client.get(f'/api/conversations/{chat}').json()['messages'][0]
        assert saved['mode'] == 'unavailable' and saved['sources'] == []
        with connect(app.state.database_path) as db:
            db.execute("UPDATE knowledge_documents SET audience='staff' WHERE id=?", (document_id,))
            row = db.execute('SELECT pages_json FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
            pages = json.loads(row['pages_json'])
            pages[1]['markdown_sections_v1'][0]['section'] = 'Different heading'
            db.execute('UPDATE knowledge_documents SET pages_json=? WHERE id=?',
                       (json.dumps(pages), document_id))
        assert client.post('/api/knowledge/answer', json={'question': 'approved devices'}).json()['sources'] == []
        saved = client.get(f'/api/conversations/{chat}').json()['messages'][0]
        assert saved['mode'] == 'unavailable' and saved['sources'] == []


def test_ocr_sections_retain_page_and_extraction_provenance(tmp_path, monkeypatch):
    from app import knowledge_documents

    monkeypatch.setenv('TAWZEEF_ENABLE_OCR', '1')
    monkeypatch.setattr(knowledge_documents, 'extract_pdf_pages_with_ocr', lambda *_: ([
        {'page': 1, 'text': '# Equipment\nUse approved devices.'},
        {'page': 2, 'text': 'More devices.\n# Overtime\nManager approval is needed.'},
    ], [2]))
    app = create_app(tmp_path / 'ocr.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        response = client.post('/api/knowledge/documents', params={
            'title': 'Synthetic policy', 'audience': 'staff', 'filename': 'ocr.pdf',
            'collection': 'handbook', 'ocr': 'true', 'pdf_markdown_sections': 'true',
        }, content=b'synthetic')
        assert response.status_code == 201 and response.json()['ocr_pages'] == [2]
        doc = fetch_authorized_documents(app.state.database_path, 'employee')[0]
        passages = _document_passages(doc)
        assert [(p.page, p.section, p.extraction) for p in passages] == [
            (1, 'Equipment', 'native'), (2, 'Equipment', 'ocr'), (2, 'Overtime', 'ocr'),
        ]
        assert passages[1].extraction_notice and passages[2].extraction_notice


def test_pdf_section_option_rejects_other_file_types(tmp_path):
    app = create_app(tmp_path / 'types.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        response = client.post('/api/knowledge/documents', params={
            'title': 'Invalid', 'audience': 'staff', 'filename': 'policy.docx', 'pdf_markdown_sections': 'true',
        }, content=b'synthetic')
        assert response.status_code == 422
        assert client.get('/api/knowledge/documents').json() == []
