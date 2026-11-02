"""OCR provenance survives the authenticated experimental hybrid endpoint."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_partial_pdf_ingestion import make_pdf

from app import hybrid_api, knowledge_documents, postgres_retrieval
from app.db import connect
from app.document_text import OCR_NOTICE
from app.main import create_app


def test_hybrid_citations_retain_provenance_and_respect_access(tmp_path, monkeypatch):
    class Index:
        def __init__(self, connection, namespace):
            self.rows = []

        def initialize(self):
            pass

        def replace(self, rows):
            self.rows = rows

        def search(self, allowed, query, limit):
            return [{'source_id': row[0], 'distance': 0.01}
                    for row in self.rows if row[0] in allowed][:limit]

    encoded = []

    class Embedder:
        def encode(self, texts, *, query):
            if not query:
                encoded.extend(texts)
            return [[1.0] for _ in texts]

    monkeypatch.setenv('TAWZEEF_ENABLE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'fixture')
    monkeypatch.setenv('TAWZEEF_ENABLE_OCR', '1')
    monkeypatch.setattr(hybrid_api, '_load_model', lambda _: Embedder())
    monkeypatch.setattr(postgres_retrieval, 'PostgresVectorIndex', Index)
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(connect=lambda **_: nullcontext(None)))
    monkeypatch.setattr(knowledge_documents, 'extract_pdf_pages_with_ocr', lambda *_: ([
        {'page': 1, 'text': 'Native leave policy.'},
        {'page': 2, 'text': 'Scanned leave policy.'},
    ], [2]))
    app = create_app(tmp_path / 'hybrid.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('hr')
        params = {'title': 'Public policy', 'audience': 'public', 'filename': 'policy.pdf',
                  'collection': 'handbook', 'ocr': 'true'}
        uploaded = client.post('/api/knowledge/documents', params=params, content=b'synthetic')
        assert uploaded.status_code == 201
        public_id = uploaded.json()['id']
        private = client.post('/api/knowledge/documents/text', json={
            'title': 'Private policy', 'text': 'Secret leave policy.', 'audience': 'hr',
            'collection': 'handbook',
        })
        assert private.status_code == 201
        legacy = client.post('/api/knowledge/documents', params={
            'title': 'Old policy', 'audience': 'public', 'filename': 'old.pdf',
            'collection': 'handbook',
        }, content=make_pdf(['text']))
        assert legacy.status_code == 201
        legacy_id = legacy.json()['id']

        login('candidate')
        endpoint = '/api/knowledge/experimental-hybrid'
        response = client.post(endpoint, json={'question': 'leave', 'collection': 'handbook'})
        assert response.status_code == 200
        sources = response.json()['sources']
        assert len(sources) == 3
        by_page = {(source['document_id'], source['page']): source for source in sources}
        assert by_page[(public_id, 1)]['extraction'] == 'native'
        assert by_page[(public_id, 1)]['extraction_notice'] is None
        assert by_page[(public_id, 2)]['extraction'] == 'ocr'
        assert by_page[(public_id, 2)]['extraction_notice'] == OCR_NOTICE
        assert by_page[(legacy_id, 1)]['extraction'] is None
        assert by_page[(legacy_id, 1)]['extraction_notice'] is None
        assert 'Secret leave policy.' not in encoded

        class MetadataChangingEmbedder(Embedder):
            def encode(self, texts, *, query):
                vectors = super().encode(texts, query=query)
                if not query:
                    with connect(app.state.database_path) as db:
                        db.execute("UPDATE knowledge_documents SET ocr_pages_json='[]' WHERE id=?", (public_id,))
                return vectors

        monkeypatch.setattr(hybrid_api, '_load_model', lambda _: MetadataChangingEmbedder())
        response = client.post(endpoint, json={'question': 'leave', 'collection': 'handbook'})
        assert response.status_code == 200
        assert all(source['document_id'] != public_id for source in response.json()['sources'])

        monkeypatch.setattr(hybrid_api, '_load_model', lambda _: Embedder())
        with connect(app.state.database_path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (public_id,))
        encoded.clear()
        response = client.post(endpoint, json={'question': 'leave', 'collection': 'handbook'})
        assert response.status_code == 200
        assert all(source['document_id'] != public_id for source in response.json()['sources'])
        assert 'Scanned leave policy.' not in encoded
