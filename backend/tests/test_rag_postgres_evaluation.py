"""Check diagnostic accounting without requiring a running PostgreSQL server."""

import pytest

pytest.importorskip('psycopg')
pytest.importorskip('numpy')
pytest.importorskip('onnxruntime')
pytest.importorskip('tokenizers')
pytest.importorskip('sqlite_vec')

from app.rag_postgres_evaluation import run, summarize


def test_refuses_to_run_without_explicit_test_database_flag(monkeypatch, tmp_path):
    monkeypatch.delenv('TAWZEEF_TEST_POSTGRES', raising=False)
    with pytest.raises(RuntimeError, match='dedicated disposable test database'):
        run(tmp_path)


def test_summary_counts_failures_and_does_not_count_http_errors_as_abstentions():
    rows = [
        {'language': 'en', 'expected': {'title': 'a'}, 'support_retrieved': True, 'top1_support': False},
        {'language': 'en', 'expected': {'title': 'b'}, 'support_retrieved': False, 'top1_support': False},
        {'language': 'ar', 'expected': {'title': 'c'}, 'support_retrieved': True, 'top1_support': True},
        {'language': 'ar', 'expected': None, 'returned': []},
        {'language': 'en', 'expected': None, 'returned': [{'title': 'irrelevant'}], 'authorization_violation': True},
        {'language': 'en', 'expected': None, 'error_http': 503},
        {'language': 'en', 'expected': None, 'returned': [], 'citation_violation': True},
    ]
    report = summarize(rows)
    assert report['support_by_language'] == {
        'en': {'hits': 1, 'top1': 0, 'total': 2},
        'ar': {'hits': 1, 'top1': 1, 'total': 1},
    }
    assert report['no_support_no_result'] == {'empty': 2, 'total': 3}
    assert report['http_errors'] == report['authorization_violations'] == report['citation_violations'] == 1
