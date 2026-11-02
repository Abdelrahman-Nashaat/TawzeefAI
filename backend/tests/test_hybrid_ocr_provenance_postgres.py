"""Opt-in OCR citation test with real PostgreSQL, pgvector and local embeddings."""

import os
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

psycopg = pytest.importorskip('psycopg')
pytest.importorskip('pgvector')
pytest.importorskip('onnxruntime')
pytest.importorskip('tokenizers')


@pytest.fixture
def isolated_vectors(monkeypatch):
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        pytest.skip('Requires a dedicated disposable PostgreSQL test database')
    from psycopg import sql

    schema = 'tawzeef_ocr_' + uuid.uuid4().hex
    with psycopg.connect(autocommit=True) as connection:
        database = connection.execute('SELECT current_database()').fetchone()[0]
        if not re.fullmatch(r'tawzeef_test_[a-f0-9]{32}', database):
            raise RuntimeError('PostgreSQL database is not a dedicated disposable test database')
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            connection.execute(sql.SQL('''CREATE TABLE {}.tawzeef_vectors (
                namespace text NOT NULL, model text NOT NULL, source_id text NOT NULL,
                revision text NOT NULL CHECK (length(revision)=64),
                embedding public.vector(384) NOT NULL, PRIMARY KEY (namespace,model,source_id)
            )''').format(sql.Identifier(schema)))
            monkeypatch.setenv('PGOPTIONS', f'-c search_path={schema},public')
            with psycopg.connect(autocommit=True) as probe:
                assert probe.execute('SELECT current_schema()').fetchone()[0] == schema
            yield
        finally:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_real_hybrid_citation_keeps_ocr_provenance_and_access(tmp_path, isolated_vectors, monkeypatch):
    from app import knowledge_documents
    from app.db import connect
    from app.document_text import OCR_NOTICE
    from app.main import create_app

    model_dir = Path(__file__).resolve().parents[2] / '.tooling' / 'multilingual-e5-small'
    if not (model_dir / 'model.onnx').is_file() or not (model_dir / 'tokenizer.json').is_file():
        pytest.skip('Requires the pinned local model files')
    monkeypatch.setenv('TAWZEEF_ENABLE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', str(model_dir))
    monkeypatch.setenv('TAWZEEF_ENABLE_OCR', '1')
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '0')
    # Synthetic text simulates an already OCR-processed upload; real OCR is tested separately.
    monkeypatch.setattr(knowledge_documents, 'extract_pdf_pages_with_ocr', lambda *_: ([
        {'page': 1, 'text': 'The annual leave policy allows fifteen days.'},
        {'page': 2, 'text': 'The annual leave policy needs manager approval.'},
    ], [2]))
    path = tmp_path / 'ocr-provenance.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        def login(role):
            response = client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            })
            assert response.status_code == 200

        login('hr')
        response = client.post('/api/knowledge/documents', params={
            'title': 'Annual leave policy', 'audience': 'staff', 'filename': 'leave.pdf',
            'collection': 'handbook', 'ocr': 'true',
        }, content=b'synthetic')
        assert response.status_code == 201
        doc_id = response.json()['id']
        with connect(path) as db:
            db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')

        login('employee')
        endpoint = '/api/knowledge/experimental-hybrid'
        response = client.post(endpoint, json={'question': 'annual leave policy', 'collection': 'handbook'})
        assert response.status_code == 200
        sources = {source['page']: source for source in response.json()['sources'] if source['document_id'] == doc_id}
        assert set(sources) == {1, 2}
        assert sources[1]['extraction'] == 'native' and sources[1]['extraction_notice'] is None
        assert sources[2]['extraction'] == 'ocr' and sources[2]['extraction_notice'] == OCR_NOTICE
        assert 'manager approval' in sources[2]['text']

        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))
        response = client.post(endpoint, json={'question': 'annual leave policy', 'collection': 'handbook'})
        assert response.status_code == 200
        assert all(source['document_id'] != doc_id for source in response.json()['sources'])
