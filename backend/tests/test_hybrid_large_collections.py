"""Hybrid retrieval includes lexical evidence beyond the vector candidate window."""

from typing import ClassVar

import pytest

pytest.importorskip('pgvector')

from app.db import connect, initialize
from app.knowledge_documents import initialize_knowledge_documents
from app.postgres_retrieval import search_postgres


class SyntheticIndex:
    """Exercise candidate fusion deterministically without requiring pgvector."""

    instances: ClassVar[list] = []
    on_search = None

    def __init__(self, connection, namespace):
        self.rows = []
        self.searches = []
        self.instances.append(self)

    def initialize(self):
        pass

    def replace(self, rows):
        self.rows = rows

    def search(self, allowed, query, limit):
        assert 1 <= limit <= 100
        self.searches.append(set(allowed))
        ranked = [
            {'source_id': source, 'distance': 0.9 if vector[0] == 9 else 0.1}
            for source, revision, vector in self.rows if allowed.get(source) == revision
        ]
        ranked.sort(key=lambda row: (row['distance'], row['source_id']))
        if self.on_search:
            self.on_search(len(self.searches))
        return ranked[:limit]


class Embedder:
    def __init__(self):
        self.seen = []

    def encode(self, texts, *, query):
        if query:
            return [[1]]
        self.seen.extend(texts)
        return [[9] if 'nebula sabbatical' in text else [1] for text in texts]


@pytest.fixture
def large_collection(tmp_path, monkeypatch):
    monkeypatch.setattr('app.postgres_retrieval.PostgresVectorIndex', SyntheticIndex)
    SyntheticIndex.instances.clear()
    SyntheticIndex.on_search = None
    path = tmp_path / 'sources.sqlite3'
    initialize(path)
    initialize_knowledge_documents(path)
    with connect(path) as db:
        hr = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES('hr','HR','hr','x')").lastrowid
        for n in range(130):
            db.execute("""INSERT INTO knowledge_documents(title,audience,collection,text,created_by)
                VALUES(?, 'staff', 'handbook', ?, ?)""", (f'Policy {n}', f'Unrelated content {n}', hr))
        target = db.execute("""INSERT INTO knowledge_documents(title,audience,collection,text,created_by)
            VALUES('Nebula sabbatical', 'staff', 'handbook', 'nebula sabbatical lasts eleven days', ?)""", (hr,)).lastrowid
        db.execute("""INSERT INTO knowledge_documents(title,audience,collection,text,created_by)
            VALUES('Secret', 'hr', 'handbook', 'PRIVATE nebula sabbatical', ?)""", (hr,))
        db.execute("""INSERT INTO knowledge_documents(title,audience,collection,text,created_by)
            VALUES('Wrong collection', 'staff', 'job_description', 'WRONG_COLLECTION nebula sabbatical', ?)""", (hr,))
    return path, target


def test_large_filtered_collection_keeps_lexical_result_outside_vector_window(large_collection):
    path, target = large_collection
    embedder = Embedder()
    result = search_postgres(path, 'employee', 'nebula sabbatical', embedder, None, collection='handbook')
    assert result['status'] == 'ok'
    assert result['sources'][0]['document_id'] == target
    assert result['sources'][0]['text'] == 'nebula sabbatical lasts eleven days'
    assert all('PRIVATE' not in text and 'WRONG_COLLECTION' not in text for text in embedder.seen)
    assert len(embedder.seen) == 131
    assert len(SyntheticIndex.instances[-1].searches) == 2


def test_lexical_only_candidate_is_rechecked_after_index_search(large_collection):
    path, target = large_collection

    def revoke(call):
        if call == 2:
            with connect(path) as db:
                db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (target,))

    SyntheticIndex.on_search = staticmethod(revoke)
    result = search_postgres(path, 'employee', 'nebula sabbatical', Embedder(), None, collection='handbook')
    assert all(row['document_id'] != target for row in result['sources'])
    assert 'eleven days' not in str(result)


def test_search_rejects_over_limit_before_embedding(large_collection):
    path, _ = large_collection
    with connect(path) as db:
        hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()['id']
        for n in range(370):
            db.execute("""INSERT INTO knowledge_documents(title,audience,collection,text,created_by)
                VALUES(?, 'staff', 'handbook', ?, ?)""", (f'Extra {n}', f'Extra content {n}', hr))
    embedder = Embedder()
    with pytest.raises(ValueError, match='Too many passages'):
        search_postgres(path, 'employee', 'nebula sabbatical', embedder, None, collection='handbook')
    assert embedder.seen == []
