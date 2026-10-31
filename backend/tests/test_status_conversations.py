import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import conversations
from app.auth import hash_password
from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    app = create_app(tmp_path / 'status.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate_id = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.pdf','synthetic','submitted')""", (candidate_id,)).lastrowid
        conversation_id = client.post('/api/conversations', json={}).json()['id']
        yield client, app.state.database_path, conversation_id, application_id


def ask(client, conversation_id, application_id):
    return client.post(f'/api/conversations/{conversation_id}/messages', json={
        'question': 'ما حالة طلبي؟', 'mode': 'application_status', 'application_id': application_id})


def test_current_stage_is_rebuilt_and_no_sensitive_data_saved(setup, monkeypatch):
    client, path, chat_id, app_id = setup
    def forbidden(*args, **kwargs):
        raise AssertionError('Status must never contact knowledge or a model')
    monkeypatch.setattr(conversations, 'answer_question', forbidden)
    first = ask(client, chat_id, app_id)
    assert first.status_code == 201
    assert first.json()['stage'] == 'applied'
    assert first.json()['sources'] == []
    with connect(path) as db:
        row = db.execute('SELECT * FROM conversation_messages').fetchone()
        assert row['answer'] == '' and json.loads(row['sources_json']) == []
        assert row['status_application_id'] == app_id
        db.execute("UPDATE applications SET status='advanced',score_json='{}' WHERE id=?", (app_id,))
    message = client.get(f'/api/conversations/{chat_id}').json()['messages'][0]
    assert message['stage'] == 'shortlisted'
    assert message['application_id'] == app_id
    assert 'score' not in message and 'resume_text' not in message
    assert client.get(f'/api/workflows/{app_id}/progress').json()['stage'] == message['stage']


def test_cannot_read_another_candidates_application_and_old_reference_is_hidden(setup):
    client, path, chat_id, app_id = setup
    assert ask(client, chat_id, app_id).status_code == 201
    with connect(path) as db:
        other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('password-123'),)).lastrowid
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, app_id))
    assert ask(client, chat_id, app_id).status_code == 404
    message = client.get(f'/api/conversations/{chat_id}').json()['messages'][0]
    assert message['mode'] == 'unavailable'
    assert 'stage' not in message and 'application_id' not in message


def test_request_contract_and_roles(setup):
    client, _, chat_id, app_id = setup
    endpoint = f'/api/conversations/{chat_id}/messages'
    assert client.post(endpoint, json={'question': 'status', 'mode': 'application_status'}).status_code == 422
    assert client.post(endpoint, json={'question': 'status', 'application_id': app_id}).status_code == 422
    assert client.post(endpoint, json={'question': 'status', 'mode': 'application_status', 'application_id': True}).status_code == 422
    for role in ('hr', 'employee'):
        assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200
        own_chat = client.post('/api/conversations', json={}).json()['id']
        assert ask(client, own_chat, app_id).status_code == 403
        assert client.get(f'/api/conversations/{chat_id}').status_code == 404


def test_session_expiry_before_commit_rolls_back(setup, monkeypatch):
    client, path, chat_id, app_id = setup
    original = conversations._owned
    calls = 0
    def expires(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)
    monkeypatch.setattr(conversations, '_owned', expires)
    assert ask(client, chat_id, app_id).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0] == 0


def test_status_only_history_does_not_load_unrelated_knowledge(setup, monkeypatch):
    client, _, chat_id, app_id = setup
    assert ask(client, chat_id, app_id).status_code == 201
    def fail_knowledge(*args):
        raise RuntimeError('knowledge unavailable')
    monkeypatch.setattr(conversations, '_authorized_sources', fail_knowledge)
    monkeypatch.setattr(conversations, '_policy_sources', fail_knowledge)
    response = client.get(f'/api/conversations/{chat_id}')
    assert response.status_code == 200
    assert response.json()['messages'][0]['stage'] == 'applied'


def test_mixed_history_still_requires_authorized_general_sources(setup, monkeypatch):
    client, path, chat_id, app_id = setup
    assert ask(client, chat_id, app_id).status_code == 201
    with connect(path) as db:
        db.execute('''INSERT INTO conversation_messages
            (conversation_id, question, answer, sources_json, mode, context_role, source_scope)
            VALUES (?, 'question', 'answer', '[]', 'abstain', 'candidate', 'general')''', (chat_id,))
    def fail_knowledge(*args):
        raise RuntimeError('knowledge unavailable')
    monkeypatch.setattr(conversations, '_authorized_sources', fail_knowledge)
    with pytest.raises(RuntimeError, match='knowledge unavailable'):
        client.get(f'/api/conversations/{chat_id}')
