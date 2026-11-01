import pytest

pytest.importorskip('psycopg')
pytest.importorskip('numpy')
pytest.importorskip('onnxruntime')
pytest.importorskip('tokenizers')
pytest.importorskip('sqlite_vec')

from evaluate_policy_hybrid import run, summarize


def test_refuses_to_run_without_disposable_database_flag(monkeypatch, tmp_path):
    monkeypatch.delenv('TAWZEEF_TEST_POSTGRES', raising=False)
    with pytest.raises(RuntimeError, match='dedicated disposable test database'):
        run(tmp_path)


def test_summary_distinguishes_retrieval_from_negative_results():
    positive = [
        {'language': 'en', 'lexical_hit': True, 'hybrid_hit': False},
        {'language': 'en', 'lexical_hit': False, 'hybrid_hit': True},
        {'language': 'ar', 'lexical_hit': False, 'hybrid_hit': False},
    ]
    negative = [
        {'hybrid_ids': [], 'forbidden_returned': []},
        {'hybrid_ids': ['irrelevant'], 'forbidden_returned': []},
        {'hybrid_ids': ['restricted'], 'forbidden_returned': ['restricted']},
    ]
    assert summarize(positive, negative) == {
        'by_language': {'en': {'total': 2, 'lexical_hits': 1, 'hybrid_hits': 1},
                        'ar': {'total': 1, 'lexical_hits': 0, 'hybrid_hits': 0}},
        'negative_total': 3, 'hybrid_empty': 1, 'hybrid_forbidden_returned': 1,
    }
