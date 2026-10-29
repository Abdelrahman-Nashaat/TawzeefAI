from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import conversations
from app.auth import hash_password
from app.db import connect
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'chat.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        yield client


def login(client, role):
    result = client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'})
    assert result.status_code == 200


def add_staff_document(client):
    login(client, 'hr')
    result = client.post('/api/knowledge/documents/text', json={
        'title': 'Nebula Leave', 'audience': 'staff',
        'text': 'Nebula leave requires seven days advance notice and manager approval.',
    })
    assert result.status_code == 201
    return result.json()['id']


def chat(client):
    result = client.post('/api/conversations', json={'title': 'Personal discussion'})
    assert result.status_code == 201
    return result.json()['id']


def ask(client, conversation_id):
    return client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'Nebula leave'})


def test_owner_isolation_including_hr_and_second_candidate(client):
    assert client.get('/api/conversations').status_code == 401
    login(client, 'candidate')
    conversation_id = chat(client)
    assert len(client.get('/api/conversations').json()) == 1
    with connect(client.app.state.database_path) as db:
        db.execute('INSERT INTO users (username,display_name,role,password_hash) VALUES (?,?,?,?)',
                   ('other', 'Other', 'candidate', hash_password('other-demo-only')))
    login(client, 'other')
    assert client.get('/api/conversations').json() == []
    assert client.get(f'/api/conversations/{conversation_id}').status_code == 404
    assert ask(client, conversation_id).status_code == 404
    for role in ('employee', 'hr'):
        login(client, role)
        assert client.get('/api/conversations').json() == []
        assert client.get(f'/api/conversations/{conversation_id}').status_code == 404
        assert ask(client, conversation_id).status_code == 404


def test_history_survives_restart_and_rechecks_deactivated_document(client):
    doc_id = add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    response = ask(client, conversation_id)
    assert response.status_code == 201
    message = response.json()
    assert message['mode'] == 'extractive'
    assert 'seven days' in message['answer']
    assert message['sources'][0]['id'] == f'db-doc-{doc_id}-p1'
    path = client.app.state.database_path
    # Reopen with the same account and persisted database.
    restarted = create_app(path, seed_demo=True)
    with TestClient(restarted) as new_client:
        login(new_client, 'employee')
        assert 'seven days' in new_client.get(f'/api/conversations/{conversation_id}').json()['messages'][0]['answer']
        login(new_client, 'hr')
        assert new_client.post(f'/api/knowledge/documents/{doc_id}/deactivate').status_code == 200
        login(new_client, 'employee')
        hidden = new_client.get(f'/api/conversations/{conversation_id}').json()['messages'][0]
        assert hidden['mode'] == 'unavailable'
        assert hidden['sources'] == [] and 'seven days' not in hidden['answer']


def test_history_hides_staff_answer_after_owner_role_is_downgraded(client):
    add_staff_document(client)
    login(client, 'employee')
    user_id = client.get('/api/auth/me').json()['id']
    conversation_id = chat(client)
    assert 'seven days' in ask(client, conversation_id).json()['answer']
    with connect(client.app.state.database_path) as db:
        db.execute("UPDATE users SET role='candidate' WHERE id=?", (user_id,))
    response = client.get(f'/api/conversations/{conversation_id}')
    assert response.status_code == 200
    message = response.json()['messages'][0]
    assert message['mode'] == 'unavailable'
    assert message['sources'] == [] and 'seven days' not in message['answer']


@pytest.mark.parametrize('change', ['audience', 'content', 'disabled_samples'])
def test_history_hides_responses_after_source_changes(client, change):
    if change == 'disabled_samples':
        login(client, 'employee')
        conversation_id = chat(client)
        result = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'annual leave'})
        assert result.json()['sources']
        login(client, 'hr')
        assert client.post('/api/knowledge/settings', json={'include_sample_policies': False}).status_code == 200
    else:
        doc_id = add_staff_document(client)
        login(client, 'employee')
        conversation_id = chat(client)
        assert ask(client, conversation_id).json()['sources']
        with connect(client.app.state.database_path) as db:
            if change == 'audience':
                db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))
            else:
                db.execute("UPDATE knowledge_documents SET text='Changed policy' WHERE id=?", (doc_id,))
    login(client, 'employee')
    message = client.get(f'/api/conversations/{conversation_id}').json()['messages'][0]
    assert message['mode'] == 'unavailable' and message['sources'] == []


@pytest.mark.parametrize('change,expected', [('source', 409), ('role', 403), ('session', 401)])
def test_rechecks_permissions_before_committing_model_result(client, monkeypatch, change, expected):
    doc_id = add_staff_document(client)
    login(client, 'employee')
    user = client.get('/api/auth/me').json()
    conversation_id = chat(client)
    original = conversations.answer_question

    def answer_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.sources
        with connect(client.app.state.database_path) as db:
            if change == 'source':
                db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc_id,))
            elif change == 'role':
                db.execute("UPDATE users SET role='candidate' WHERE id=?", (user['id'],))
            else:
                db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        return result

    monkeypatch.setattr(conversations, 'answer_question', answer_then_change)
    response = ask(client, conversation_id)
    assert response.status_code == expected
    with connect(client.app.state.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0] == 0


def test_concurrent_messages_persist_without_losing_records(client):
    add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ask(client, conversation_id).status_code, range(2)))
    assert results == [201, 201]
    assert len(client.get(f'/api/conversations/{conversation_id}').json()['messages']) == 2
