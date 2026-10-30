"""Real PostgreSQL and model integration for DOCX section citations."""

import json
import os
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

psycopg = pytest.importorskip('psycopg')
pytest.importorskip('pgvector')
pytest.importorskip('onnxruntime')
pytest.importorskip('tokenizers')


@pytest.fixture
def pg_schema(monkeypatch):
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        pytest.skip('Requires an explicitly configured PostgreSQL test database')
    from psycopg import sql

    schema = 'tawzeef_docx_' + uuid.uuid4().hex
    with psycopg.connect(autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        monkeypatch.setenv('PGOPTIONS', f'-c search_path={schema},public')
        try:
            yield schema
        finally:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_docx_sections_hybrid_citations_and_revocation(tmp_path, pg_schema, monkeypatch):
    from app import hybrid_api
    from app.db import connect
    from app.main import create_app
    from app.semantic_experiment import LocalE5

    model_dir = Path(__file__).resolve().parents[2] / '.tooling' / 'multilingual-e5-small'
    if not (model_dir / 'model.onnx').is_file() or not (model_dir / 'tokenizer.json').is_file():
        pytest.skip('Requires the pinned local model files')
    monkeypatch.setenv('TAWZEEF_ENABLE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', str(model_dir))
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '0')

    model = LocalE5(model_dir)
    path = tmp_path / 'docx.sqlite3'
    document = Document()
    document.add_heading('Annual leave', level=1)
    document.add_paragraph('Employees receive 21 days of annual leave per year.')
    document.add_heading('Remote work', level=1)
    document.add_paragraph('Remote work needs manager approval.')
    data = BytesIO()
    document.save(data)

    with TestClient(create_app(path, seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('hr')
        uploaded = client.post('/api/knowledge/documents', params={
            'title': 'Staff handbook', 'audience': 'public', 'filename': 'policy.docx',
            'collection': 'handbook',
        }, content=data.getvalue())
        assert uploaded.status_code == 201
        document_id = uploaded.json()['id']
        login('candidate')
        monkeypatch.setattr(hybrid_api, '_load_model', lambda directory: model)
        endpoint = '/api/knowledge/experimental-hybrid'
        question = {'question': 'How many days of annual leave do employees receive?'}
        response = client.post(endpoint, json=question)
        assert response.status_code == 200
        cited = next(source for source in response.json()['sources'] if source['section'] == 'Annual leave')
        assert cited['document_id'] == document_id
        assert cited['page'] is None
        assert '21 days of annual leave' in cited['text']

        with connect(path) as db:
            db.execute('UPDATE knowledge_documents SET audience = ? WHERE id = ?', ('hr', document_id))
        denied = client.post(endpoint, json=question)
        assert denied.status_code == 200 and denied.json()['sources'] == []

        with connect(path) as db:
            db.execute('UPDATE knowledge_documents SET audience = ? WHERE id = ?', ('public', document_id))

        class ChangeSectionDuringEmbedding:
            def __init__(self):
                self.changed = False

            def encode(self, texts, *, query):
                result = model.encode(texts, query=query)
                if not query and not self.changed:
                    self.changed = True
                    with connect(path) as db:
                        row = db.execute('SELECT text, sections_json FROM knowledge_documents WHERE id=?',
                                         (document_id,)).fetchone()
                        sections = json.loads(row['sections_json'])
                        sections[0]['section'] = 'Leave policy'
                        sections[0]['text'] = sections[0]['text'].replace('Annual leave', 'Leave policy', 1)
                        text = '\n'.join(section['text'] for section in sections)
                        db.execute('UPDATE knowledge_documents SET text=?, sections_json=? WHERE id=?',
                                   (text, json.dumps(sections, ensure_ascii=False), document_id))
                return result

        monkeypatch.setattr(hybrid_api, '_load_model', lambda directory: ChangeSectionDuringEmbedding())
        changed = client.post(endpoint, json=question)
        assert changed.status_code == 200
        assert changed.json()['sources'] == []
