import json

import pytest

np = pytest.importorskip('numpy')
pytest.importorskip('onnxruntime')
pytest.importorskip('tokenizers')
pytest.importorskip('sqlite_vec')

from app.db import connect, initialize
from app.knowledge_documents import initialize_knowledge_documents
from app.semantic_experiment import search_hybrid


class SyntheticEmbedder:
    def __init__(self, on_passages=None):
        self.seen = []
        self.on_passages = on_passages

    def encode(self, texts, *, query):
        self.seen.extend(texts)
        if not query and self.on_passages:
            self.on_passages()
            self.on_passages = None
        return np.array([[0.0, 1.0, 0.0] if 'unrelated' in text else [1.0, 0.0, 0.0]
                         for text in texts], dtype=np.float32)


def make_db(tmp_path):
    path = tmp_path / 'db.sqlite3'
    initialize(path)
    initialize_knowledge_documents(path)
    with connect(path) as db:
        hr = db.execute("INSERT INTO users (username,display_name,role,password_hash) VALUES ('hr','HR','hr','x')").lastrowid
        def document(title, audience, text, pages=None):
            return db.execute('''INSERT INTO knowledge_documents (title,audience,text,created_by,pages_json)
                VALUES (?, ?, ?, ?, ?)''', (title, audience, text, hr, json.dumps(pages) if pages else None)).lastrowid
        public = document('Leave policy', 'public', 'Leave\nAnnual vacation is 21 days.',
                          [{'page': 1, 'text': 'Leave'}, {'page': 2, 'text': 'Annual vacation is 21 days.'}])
        private = document('HR only', 'hr', 'SECRET_PRIVATE phrase')
    return path, public, private


def test_hybrid_filters_before_embedding_and_keeps_page_citations(tmp_path):
    path, public, _ = make_db(tmp_path)
    embedder = SyntheticEmbedder()
    result = search_hybrid(path, 'candidate', 'annual vacation days', embedder, min_similarity=0.8)
    assert result['status'] == 'ok'
    assert any(source['page'] == 2 and source['document_id'] == public for source in result['sources'])
    assert 'SECRET_PRIVATE' not in str(embedder.seen)
    assert 'SECRET_PRIVATE' not in str(result)


def test_hybrid_abstains_for_unrelated_query_or_unauthorized_role(tmp_path):
    path, _, _ = make_db(tmp_path)
    assert search_hybrid(path, 'candidate', 'unrelated query', SyntheticEmbedder(), min_similarity=0.8)['status'] == 'abstain'
    assert search_hybrid(path, 'unknown', 'annual vacation', SyntheticEmbedder(), min_similarity=0.8)['sources'] == []


def test_document_deactivated_during_embedding_never_returned(tmp_path):
    path, public, _ = make_db(tmp_path)
    def deactivate():
        with connect(path) as db:
            db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (public,))
    result = search_hybrid(path, 'candidate', 'annual vacation', SyntheticEmbedder(deactivate), min_similarity=0.8)
    assert result['status'] == 'abstain'
    assert result['sources'] == []
