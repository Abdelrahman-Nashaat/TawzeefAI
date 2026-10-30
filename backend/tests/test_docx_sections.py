import hashlib
import json
from dataclasses import asdict
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.conversations import _digest
from app.db import connect
from app.document_text import extract_document_text, extract_docx_sections
from app.knowledge import Source, _document_passages
from app.main import create_app


def document_bytes(with_headings=True):
    document = Document()
    document.add_paragraph('Preamble')
    document.add_paragraph('الإجازة السنوية', style='Heading 1' if with_headings else None)
    document.add_paragraph('Annual leave is 21 days per year.')
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = 'Table text remains in the same section.'
    document.add_paragraph('Remote work', style='Heading 2' if with_headings else None)
    document.add_paragraph('Remote work requires manager approval.')
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_extract_headings_preserves_exact_text_tables_and_v1_fallback():
    data = document_bytes()
    text, sections = extract_docx_sections('sample.docx', data)
    assert text == extract_document_text('sample.docx', data)
    assert [s['section'] for s in sections] == [None, 'الإجازة السنوية', 'Remote work']
    assert 'Table text' in sections[1]['text']
    assert '\n'.join(s['text'] for s in sections) == text
    assert extract_docx_sections('plain.docx', document_bytes(False))[1] is None
    heading_with_break = Document()
    heading_with_break.add_paragraph('Annual\nleave', style='Heading 1')
    heading_with_break.add_paragraph('Annual leave is 21 days.')
    buffer = BytesIO()
    heading_with_break.save(buffer)
    extracted, parts = extract_docx_sections('linebreak.docx', buffer.getvalue())
    result = _document_passages({'id': 2, 'title': 'Policy', 'text': extracted,
                                 'sections_json': json.dumps(parts)})
    assert result[0].section == 'Annual\nleave'


def test_invalid_sections_fail_closed_and_legacy_digest_is_unchanged():
    source = Source('old', 'Title', 'Text')
    old = {k: v for k, v in asdict(source).items() if k not in ('page', 'section')}
    expected = hashlib.sha256(json.dumps(old, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert _digest(source) == expected
    assert _digest(Source('old', 'Title', 'Text', section='Section')) != expected
    document = {'id': 1, 'title': 'Doc', 'text': 'Heading\nBody', 'pages_json': None,
                'sections_json': json.dumps([{'section': 'Wrong', 'text': 'Heading\nBody'}])}
    assert _document_passages(document) == []
    document['sections_json'] = json.dumps([{'section': 'Heading', 'text': 'Heading\nBody'}])
    document['pages_json'] = json.dumps([{'page': 1, 'text': 'Heading\nBody'}])
    assert _document_passages(document) == []


def test_vector_revision_remains_compatible_for_legacy_sources():
    pytest.importorskip('pgvector')
    pytest.importorskip('psycopg')
    from app.postgres_retrieval import _revision

    source = Source('old', 'Title', 'Text')
    original = json.dumps([source.id, source.title, source.text, source.page], ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')
    assert _revision(source) == hashlib.sha256(original).hexdigest()
    assert _revision(Source('old', 'Title', 'Text', section='Section')) != _revision(source)


def test_uploaded_sections_retrieval_and_conversation_authorization(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'test.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200
        login('hr')
        uploaded = client.post('/api/knowledge/documents', params={
            'title': 'Work rules', 'audience': 'public', 'filename': 'policy.docx',
        }, content=document_bytes())
        assert uploaded.status_code == 201
        doc_id = uploaded.json()['id']
        login('candidate')
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'question': 'remote work approval'}).json()
        assert any(s['section'] == 'Remote work' and s['page'] is None for s in answer['sources'])
        with connect(path) as db:
            db.execute('UPDATE knowledge_documents SET audience = ? WHERE id = ?', ('hr', doc_id))
        stored = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert stored['mode'] == 'unavailable' and stored['sources'] == []
