"""The PostgreSQL demo serves only its own browser assets."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import mode  # noqa: F401

from app.main import create_app


def test_postgres_demo_assets_and_isolation(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert 'Synthetic CV' in page.text
        assert '/postgres-demo.js' in page.text
        assert page.headers['cache-control'] == 'no-store'
        script = client.get('/postgres-demo.js')
        assert script.status_code == 200
        assert 'textContent' in script.text
        assert 'innerHTML' not in script.text
        assert client.get('/postgres-demo.css').status_code == 200
        assert client.get('/assets/app.js').status_code == 404
        assert client.get('/api/applications').status_code == 401
