"""Liveness and database readiness are reported independently."""
import psycopg
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, mode  # noqa: F401

from app.main import create_app


def test_ready_checks_schema_and_hides_database_errors(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg

    with TestClient(create_app(seed_demo=True)) as client:
        response = client.get('/api/ready')
        assert response.status_code == 200 and response.json() == {'status': 'ready'}
        assert response.headers['cache-control'] == 'no-store'
        with db_for(mode) as db:
            db.execute('UPDATE metadata SET version=999')
        response = client.get('/api/ready')
        assert response.status_code == 503 and response.json() == {'status': 'unavailable'}
        assert client.get('/api/health').status_code == 200
        def unavailable(*args, **kwargs):
            raise psycopg.OperationalError('private connection details')
        with monkeypatch.context() as isolated:
            isolated.setattr(pg.psycopg, 'connect', unavailable)
            response = client.get('/api/ready')
            assert response.status_code == 503 and 'private connection details' not in response.text
