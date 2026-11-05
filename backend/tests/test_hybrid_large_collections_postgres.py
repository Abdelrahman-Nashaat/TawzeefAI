"""Optional live-pgvector coverage of hybrid fusion beyond 100 passages."""

import pytest

pytest.importorskip('psycopg')
pytest.importorskip('pgvector')

from test_postgres_vectors import pg_connection  # noqa: F401

from app.db import connect, initialize
from app.knowledge_documents import initialize_knowledge_documents
from app.postgres_retrieval import search_postgres


def test_large_collection_lexical_only_hit_with_real_pgvector(tmp_path, pg_connection):  # noqa: F811
    path = tmp_path / 'large.sqlite3'
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

    class Embedder:
        def __init__(self):
            self.seen = []

        def encode(self, texts, *, query):
            if query:
                return [[1.0] + [0.0] * 383]
            self.seen.extend(texts)
            return [([0.0, 1.0] if 'nebula sabbatical' in text else [1.0, 0.0]) + [0.0] * 382
                    for text in texts]

    embedder = Embedder()
    result = search_postgres(path, 'employee', 'nebula sabbatical', embedder, pg_connection, collection='handbook')
    assert result['sources'][0]['document_id'] == target
    assert result['sources'][0]['text'] == 'nebula sabbatical lasts eleven days'
    assert all('PRIVATE' not in text for text in embedder.seen)
