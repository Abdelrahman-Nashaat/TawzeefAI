"""Conversation ownership, source revocation, and transaction boundaries."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_conversation_persists_and_revalidates_sources_and_owner(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as hr, TestClient(app) as candidate, TestClient(app) as other:
        login(hr, 'hr')
        login(candidate, 'candidate')
        login(other, 'candidate2')
        doc = hr.post('/api/knowledge/documents/text', json={
            'title': 'Leave', 'text': 'Annual leave requires manager approval.',
            'audience': 'public', 'collection': 'handbook', 'page': 2}).json()
        secret = hr.post('/api/knowledge/documents/text', json={
            'title': 'Private leave', 'text': 'Annual leave is privately adjusted.',
            'audience': 'hr', 'collection': 'handbook'}).json()
        created = candidate.post('/api/knowledge/conversations')
        assert created.status_code == 201
        cid = created.json()['id']
        response = candidate.post(f'/api/knowledge/conversations/{cid}/turns', json={'question': 'annual leave'})
        assert response.status_code == 201
        assert [s['title'] for s in response.json()['sources']] == ['Leave']
        assert other.get(f'/api/knowledge/conversations/{cid}/turns').status_code == 404
        assert hr.get(f'/api/knowledge/conversations/{cid}/turns').status_code == 404
        assert candidate.get('/api/knowledge/conversations').json()['items'][0]['id'] == cid
        with db_for(mode) as db:
            stored = db.execute('SELECT sources_json FROM knowledge_conversation_turns').fetchone()['sources_json']
            assert 'manager approval' not in stored and 'Private leave' not in stored
        assert hr.post(f"/api/knowledge/documents/{doc['id']}/deactivate").status_code == 200
        item = candidate.get(f'/api/knowledge/conversations/{cid}/turns').json()['items'][0]
        assert item['mode'] == 'source_unavailable' and item['sources'] == []
        assert hr.post(f"/api/knowledge/documents/{secret['id']}/deactivate").status_code == 200
    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'candidate')
        assert client.get(f'/api/knowledge/conversations/{cid}/turns').json()['items'][0]['question'] == 'annual leave'


def test_save_rolls_back_on_expiry_and_insertion_failure(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_conversations as conv
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as client:
        login(client, 'candidate')
        cid = client.post('/api/knowledge/conversations').json()['id']
        original = conv._rank_lexical
        def expire(*args):
            result = original(*args)
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 86400))
            return result
        monkeypatch.setattr(conv, '_rank_lexical', expire)
        assert client.post(f'/api/knowledge/conversations/{cid}/turns', json={'question': 'leave'}).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversation_turns').fetchone()['n'] == 0
        monkeypatch.setattr(pg_app, 'time', time)
        monkeypatch.setattr(conv, '_rank_lexical', original)
        with db_for(mode) as db:
            db.execute("ALTER TABLE knowledge_conversation_turns ADD CONSTRAINT deny_save CHECK (question <> 'leave')")
        assert client.post(f'/api/knowledge/conversations/{cid}/turns', json={'question': 'leave'}).status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversation_turns').fetchone()['n'] == 0


def test_changed_source_and_role_hide_previously_saved_excerpts(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        doc = hr.post('/api/knowledge/documents/text', json={
            'title': 'Private policy', 'text': 'Annual leave has a confidential exception.',
            'audience': 'hr', 'collection': 'handbook'}).json()
        cid = hr.post('/api/knowledge/conversations').json()['id']
        assert hr.post(f'/api/knowledge/conversations/{cid}/turns', json={'question': 'annual leave'}).status_code == 201
        with db_for(mode) as db:
            db.execute('UPDATE knowledge_documents SET body=%s WHERE id=%s', ('Changed policy', doc['id']))
        turn = hr.get(f'/api/knowledge/conversations/{cid}/turns').json()['items'][0]
        assert turn['mode'] == 'source_unavailable' and turn['sources'] == []
        with db_for(mode) as db:
            db.execute("UPDATE users SET role='employee' WHERE username='hr'")
        response = hr.get(f'/api/knowledge/conversations/{cid}/turns')
        assert response.status_code in (403, 404)
        assert 'confidential exception' not in response.text


def test_delete_is_owner_scoped_and_cascades_saved_turns(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as owner, TestClient(create_app(seed_demo=True)) as other:
        login(owner, 'candidate')
        login(other, 'candidate2')
        cid = owner.post('/api/knowledge/conversations').json()['id']
        assert owner.post(f'/api/knowledge/conversations/{cid}/turns', json={'question': 'annual leave'}).status_code == 201
        assert other.delete(f'/api/knowledge/conversations/{cid}').status_code == 404
        assert len(owner.get(f'/api/knowledge/conversations/{cid}/turns').json()['items']) == 1
        assert owner.delete(f'/api/knowledge/conversations/{cid}').status_code == 204
        assert owner.get(f'/api/knowledge/conversations/{cid}/turns').status_code == 404
        assert owner.delete(f'/api/knowledge/conversations/{cid}').status_code == 404
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversations').fetchone()['n'] == 0
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversation_turns').fetchone()['n'] == 0


def test_delete_rolls_back_if_session_expires_after_lock(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_conversations as conv
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'candidate')
        cid = client.post('/api/knowledge/conversations').json()['id']
        assert client.post(f'/api/knowledge/conversations/{cid}/turns', json={'question': 'annual leave'}).status_code == 201
        original = conv._owner
        def expire_after_lock(*args):
            original(*args)
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 86400))
        monkeypatch.setattr(conv, '_owner', expire_after_lock)
        assert client.delete(f'/api/knowledge/conversations/{cid}').status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversations').fetchone()['n'] == 1
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversation_turns').fetchone()['n'] == 1
