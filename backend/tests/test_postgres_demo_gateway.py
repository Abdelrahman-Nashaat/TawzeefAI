"""Configured Docker gateway access is restricted to one peer."""
import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import mode  # noqa: F401

from app.main import create_app


def test_configured_demo_gateway_is_narrowly_scoped(mode, monkeypatch):  # noqa: F811
    monkeypatch.setenv('TAWZEEF_DEMO_GATEWAY_IP', '172.29.174.1')
    app = create_app(seed_demo=True)
    with TestClient(app, client=('172.29.174.1', 1000)) as gateway:
        assert gateway.get('/api/health').status_code == 200
        assert gateway.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'},
                            headers={'origin': 'https://invalid.example'}).status_code == 403
    with TestClient(app, client=('172.29.174.2', 1000)) as other:
        assert other.get('/api/health').status_code == 403


def test_demo_gateway_rejects_public_address(mode, monkeypatch):  # noqa: F811
    monkeypatch.setenv('TAWZEEF_DEMO_GATEWAY_IP', '8.8.8.8')
    with pytest.raises(RuntimeError, match='private non-loopback'):
        create_app(seed_demo=True)

