"""Real pgvector verifies cache invalidation and authorized-source filtering."""
import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, mode  # noqa: F401

from app.knowledge import Source
from app.main import create_app
from app.postgres_knowledge import _semantic_candidates


class CountingModel:
    def __init__(self):
        self.passage_batches = []
        self.query_count = 0

    def encode(self, texts, *, query):
        if query:
            self.query_count += 1
        else:
            self.passage_batches.append(list(texts))
        return [[1.0] + [0.0] * 383 for _ in texts]


def test_cache_reuses_only_current_authorized_passages(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    with TestClient(create_app(seed_demo=True)), db_for(mode) as db:
        if not db.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            pytest.skip('A disposable PostgreSQL database with pgvector is required')
        model = CountingModel()
        public = Source('public', 'Policy', 'Annual leave requires approval.')
        private = Source('private', 'HR', 'Private policy exception.')
        _semantic_candidates(db, [public, private], 'annual leave', model)
        assert model.passage_batches == [[public.text, private.text]]
        result = _semantic_candidates(db, [public], 'annual leave', model)
        assert [source.id for _, source in result] == ['public']
        assert model.passage_batches == [[public.text, private.text]]
        changed = Source('public', 'Policy', 'Annual leave requires manager approval.')
        _semantic_candidates(db, [changed], 'annual leave', model)
        assert model.passage_batches[-1] == [changed.text]
        monkeypatch.setattr(knowledge, '_PASSAGE_EMBEDDING_VERSION', 'future-test-version')
        _semantic_candidates(db, [changed], 'annual leave', model)
        assert model.passage_batches[-1] == [changed.text]
        assert len(model.passage_batches) == 3 and model.query_count == 4


def test_failed_refresh_rolls_back_cache(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)):
        with db_for(mode) as db:
            if not db.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
                pytest.skip('A disposable PostgreSQL database with pgvector is required')
            model = CountingModel()
            source = Source('doc', 'Policy', 'Original text.')
            _semantic_candidates(db, [source], 'query', model)
            original_revision = db.execute("SELECT revision FROM knowledge_vectors WHERE source_id='doc'").fetchone()['revision']
        with db_for(mode) as db:
            class BrokenModel(CountingModel):
                def encode(self, texts, *, query):
                    if query:
                        raise ValueError('Synthetic model failure')
                    return super().encode(texts, query=query)
            with pytest.raises(ValueError, match='Synthetic model failure'):
                _semantic_candidates(db, [Source('doc', 'Policy', 'Changed text.')], 'query', BrokenModel())
            db.rollback()
        with db_for(mode) as db:
            assert db.execute("SELECT revision FROM knowledge_vectors WHERE source_id='doc'").fetchone()['revision'] == original_revision
