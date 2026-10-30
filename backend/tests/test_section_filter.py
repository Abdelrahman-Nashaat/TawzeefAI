import json

import pytest
from pydantic import ValidationError

from app.hybrid_api import HybridQuestion
from app.postgres_retrieval import search_postgres


def test_section_input_is_trimmed_and_bounded():
    assert HybridQuestion(question='leave', section='  الإجازات  ').section == 'الإجازات'
    for value in ('', '   ', 'x' * 201, 'foo\x00bar'):
        with pytest.raises(ValidationError):
            HybridQuestion(question='leave', section=value)


def test_filter_precedes_embedding_and_rechecks_source(monkeypatch, tmp_path):
    from app import postgres_retrieval as retrieval

    docs = [{'id': 1, 'title': 'Manual', 'audience': 'public', 'collection': 'handbook', 'job_id': None,
             'text': 'Leave\nAnnual leave 21 days\nRemote\nWork from home', 'pages_json': None,
             'sections_json': json.dumps([{'section': 'Leave', 'text': 'Leave\nAnnual leave 21 days'},
                                          {'section': 'Remote', 'text': 'Remote\nWork from home'}])}]
    seen = []
    namespaces = []

    class Index:
        def __init__(self, connection, namespace):
            namespaces.append(namespace)

        def initialize(self):
            pass

        def replace(self, rows):
            self.rows = rows

        def search(self, allowed, query, limit):
            return [{'source_id': row[0], 'distance': 0.01} for row in self.rows if row[0] in allowed]

    class Embedder:
        def __init__(self, mutate=False):
            self.mutate = mutate

        def encode(self, texts, *, query):
            if not query:
                seen.extend(texts)
                if self.mutate:
                    docs[0] = {**docs[0], 'sections_json': json.dumps([
                        {'section': 'Updated', 'text': 'Updated\nAnnual leave 21 days'},
                        {'section': 'Remote', 'text': 'Remote\nWork from home'}]),
                        'text': 'Updated\nAnnual leave 21 days\nRemote\nWork from home'}
            return [[1.0] for _ in texts]

    monkeypatch.setattr(retrieval, 'PostgresVectorIndex', Index)
    monkeypatch.setattr(retrieval, 'fetch_authorized_documents', lambda path, role: [dict(d) for d in docs])
    path = tmp_path / 'x.sqlite'
    unfiltered = search_postgres(path, 'employee', 'annual leave', Embedder(), None)
    assert len(unfiltered['sources']) == 2
    seen.clear()
    filtered = search_postgres(path, 'employee', 'annual leave', Embedder(), None, section='Leave')
    assert [row['section'] for row in filtered['sources']] == ['Leave']
    assert seen == ['Leave\nAnnual leave 21 days']
    assert namespaces[0] != namespaces[1]
    assert search_postgres(path, 'employee', 'annual leave', Embedder(True), None, section='Leave')['sources'] == []
    assert len(namespaces[1]) <= 120


def test_section_filter_does_not_return_other_source_types(monkeypatch, tmp_path):
    from app import postgres_retrieval as retrieval

    docs = [{'id': 1, 'title': 'Old', 'audience': 'public', 'collection': 'legacy_unclassified', 'job_id': None,
             'text': 'Annual leave 21 days', 'pages_json': None, 'sections_json': None}]
    class Index:
        def __init__(self, connection, namespace): pass
        def initialize(self): pass
        def replace(self, rows): assert rows == []
    monkeypatch.setattr(retrieval, 'PostgresVectorIndex', Index)
    monkeypatch.setattr(retrieval, 'fetch_authorized_documents', lambda path, role: docs)
    class Embedder:
        def encode(self, texts, *, query): raise AssertionError('No text should reach model')
    assert search_postgres(tmp_path / 'x', 'candidate', 'annual leave', Embedder(), None,
                           section='Leave')['sources'] == []
