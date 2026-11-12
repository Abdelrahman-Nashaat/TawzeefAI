"""Role-filtered knowledge access against an isolated PostgreSQL schema."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_knowledge_access_and_persistence(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as hr, TestClient(app) as candidate, TestClient(app) as employee:
        for client, name in ((hr, 'hr'), (candidate, 'candidate'), (employee, 'employee')):
            login(client, name)
        def create(title, text, audience, collection='handbook'):
            return hr.post('/api/knowledge/documents/text', json={'title': title, 'text': text,
                'audience': audience, 'collection': collection, 'page': 3})
        public = create('Public leave', 'Annual leave requires manager approval.', 'public')
        staff = create('Staff leave', 'Annual leave is twenty working days.', 'staff')
        secret = create('HR leave', 'Annual leave exception is approved privately.', 'hr')
        for response in (public, staff, secret):
            assert response.status_code == 201, response.text
        assert create('Unsafe archive', 'Annual leave archive pattern.', 'public', 'past_hire_patterns').status_code == 422
        assert candidate.post('/api/knowledge/documents/text', json={'title': 'x', 'text': 'x',
            'audience': 'public', 'collection': 'handbook'}).status_code == 403
        question = {'question': 'annual leave'}
        for client, expected in ((candidate, {'Public leave'}), (employee, {'Public leave', 'Staff leave'}),
                                 (hr, {'Public leave', 'Staff leave', 'HR leave'})):
            result = client.post('/api/knowledge/answer', json=question)
            assert result.status_code == 200, result.text
            assert {source['title'] for source in result.json()['sources']} == expected
            assert all(source['page'] == 3 for source in result.json()['sources'])
        assert candidate.post('/api/knowledge/answer', json={'question': 'unrelated zebras'}).json()['mode'] == 'abstain'
        assert hr.post(f"/api/knowledge/documents/{public.json()['id']}/deactivate").status_code == 200
        assert candidate.post('/api/knowledge/answer', json=question).json()['mode'] == 'abstain'
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_audit').fetchone()['n'] == 4
    with TestClient(create_app(seed_demo=True)) as restarted:
        login(restarted, 'employee')
        sources = restarted.post('/api/knowledge/answer', json={'question': 'annual leave'}).json()['sources']
        assert {source['title'] for source in sources} == {'Staff leave'}


def test_document_audit_failure_rolls_back(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        login(hr, 'hr')
        with db_for(mode) as db:
            db.execute("ALTER TABLE knowledge_audit ADD CONSTRAINT reject_created CHECK (action <> 'created')")
        result = hr.post('/api/knowledge/documents/text', json={'title': 'Example', 'text': 'Annual leave',
                         'audience': 'public', 'collection': 'handbook'})
        assert result.status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_documents').fetchone()['n'] == 0


def test_expired_session_does_not_receive_ranked_sources(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_experimental as pg_app
    import app.postgres_knowledge as knowledge

    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        result = hr.post('/api/knowledge/documents/text', json={'title': 'Annual leave',
                         'text': 'Annual leave requires approval.', 'audience': 'public', 'collection': 'handbook'})
        assert result.status_code == 201
        original = knowledge._rank_lexical
        def expire(*args):
            ranked = original(*args)
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            return ranked
        monkeypatch.setattr(knowledge, '_rank_lexical', expire)
        response = candidate.post('/api/knowledge/answer', json={'question': 'annual leave'})
        assert response.status_code == 401
        assert 'Annual leave requires approval' not in response.text
