"""Evaluation schema creation and collision handling on real PostgreSQL."""
import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, mode  # noqa: F401

from app.postgres_experimental import create_postgres_app


def test_require_new_schema_does_not_reuse_or_claim_existing_schema(mode):  # noqa: F811
    created = []
    with TestClient(create_postgres_app(seed_demo=True, require_new_schema=True,
                                        on_schema_created=created.append)):
        assert len(created) == 1
    with db_for(mode) as db:
        db.execute("INSERT INTO knowledge_documents(title,body,audience,collection,created_by) "
                   "SELECT 'sentinel','existing','hr','handbook',id FROM users WHERE username='hr'")
    claimed = []
    with (pytest.raises(RuntimeError, match='schema already exists'),
          TestClient(create_postgres_app(seed_demo=True, require_new_schema=True,
                                        on_schema_created=claimed.append))):
        pass
    assert claimed == []
    with db_for(mode) as db:
        assert db.execute("SELECT count(*) AS n FROM knowledge_documents WHERE title='sentinel'").fetchone()['n'] == 1
