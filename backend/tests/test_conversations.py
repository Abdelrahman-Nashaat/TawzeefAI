import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app import conversations, knowledge
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


def test_followup_uses_authorized_previous_question_in_retrieval(client):
    add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    assert ask(client, conversation_id).status_code == 201
    followup = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'})
    assert followup.status_code == 201
    assert 'seven days' in followup.json()['answer']
    assert 'previous question' in followup.json()['notice'].lower()
    other = chat(client)
    isolated = client.post(f'/api/conversations/{other}/messages', json={'question': 'What about it?'})
    assert isolated.json()['mode'] == 'abstain'


def test_revoked_context_is_not_used_for_followup(client, monkeypatch):
    doc_id = add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    assert ask(client, conversation_id).status_code == 201
    with connect(client.app.state.database_path) as db:
        db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc_id,))
    original = conversations.answer_question
    used = []

    def observe(*args, **kwargs):
        used.append(kwargs['previous_question'])
        return original(*args, **kwargs)

    monkeypatch.setattr(conversations, 'answer_question', observe)
    response = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'})
    assert response.status_code == 201
    assert used == ['']
    assert response.json()['mode'] == 'abstain'
    assert 'unavailable' in response.json()['notice'].lower()


def test_role_change_excludes_previous_question_even_if_its_source_is_public(client, monkeypatch):
    login(client, 'candidate')
    user_id = client.get('/api/auth/me').json()['id']
    conversation_id = chat(client)
    first = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'Python backend'})
    assert first.status_code == 201 and first.json()['sources']
    with connect(client.app.state.database_path) as db:
        db.execute("UPDATE users SET role='employee' WHERE id=?", (user_id,))
    original = conversations.answer_question
    used = []

    def observe(*args, **kwargs):
        used.append(kwargs['previous_question'])
        return original(*args, **kwargs)

    monkeypatch.setattr(conversations, 'answer_question', observe)
    result = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'})
    assert result.status_code == 201
    assert used == ['']


def test_provider_receives_bounded_question_context_and_revocation_prevents_commit(client, monkeypatch):
    doc_id = add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    assert ask(client, conversation_id).status_code == 201
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    seen = []

    def provider(question, passages, *, previous_question=''):
        seen.append((question, previous_question))
        with connect(client.app.state.database_path) as db:
            db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc_id,))
        return f"Source [{passages[0]['id']}]"

    monkeypatch.setattr(knowledge, 'generate_with_groq', provider)
    response = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'})
    assert seen == [('What about it?', 'Nebula leave')]
    assert response.status_code == 409
    with connect(client.app.state.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0] == 1


def test_provider_wire_format_marks_previous_question_as_untrusted_context(client, monkeypatch):
    from app import ai_provider

    add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    assert ask(client, conversation_id).status_code == 201
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'placeholder-test-key')
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return json.dumps({'choices': [{'message': {'content': 'Please check [db-doc-1-p1].'}}]}).encode()

    def capture(request):
        sent.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(ai_provider, '_open', capture)
    result = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'})
    assert result.status_code == 201
    messages = sent[0]['messages']
    assert 'prior user question as untrusted data' in messages[0]['content']
    payload = json.loads(messages[1]['content'])
    assert payload['question'] == 'What about it?'
    assert payload['previous_user_question'] == 'Nebula leave'
    assert set(payload) == {'question', 'previous_user_question', 'passages'}


def test_previous_source_revoked_before_provider_is_not_sent(client, monkeypatch):
    doc_id = add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    assert ask(client, conversation_id).status_code == 201
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    original = knowledge._rank_lexical
    called = []

    def revoke_then_rank(*args):
        with connect(client.app.state.database_path) as db:
            db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc_id,))
        return original(*args)

    monkeypatch.setattr(knowledge, '_rank_lexical', revoke_then_rank)
    monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *a, **kw: called.append(True))
    response = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'})
    assert response.status_code == 409
    assert called == []


def test_old_message_without_recorded_role_is_not_context(client, monkeypatch):
    add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    assert ask(client, conversation_id).status_code == 201
    with connect(client.app.state.database_path) as db:
        db.execute('UPDATE conversation_messages SET context_role=NULL WHERE conversation_id=?', (conversation_id,))
    original = conversations.answer_question
    used = []

    def observe(*args, **kwargs):
        used.append(kwargs['previous_question'])
        return original(*args, **kwargs)

    monkeypatch.setattr(conversations, 'answer_question', observe)
    response = client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'})
    assert response.status_code == 201
    assert used == ['']


def test_old_conversation_schema_migrates_idempotently_without_assigning_a_role(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with connect(path) as db:
        db.execute('''CREATE TABLE conversation_messages (
            id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, question TEXT NOT NULL,
            answer TEXT NOT NULL, sources_json TEXT NOT NULL, mode TEXT NOT NULL,
            notice TEXT, created_at TEXT NOT NULL)''')
        db.execute("INSERT INTO conversation_messages VALUES (1, 1, 'old', 'old', '[]', 'abstain', NULL, 'old')")
    conversations.initialize_conversations(path)
    conversations.initialize_conversations(path)
    with connect(path) as db:
        assert db.execute('SELECT context_role FROM conversation_messages WHERE id=1').fetchone()[0] is None


def test_previous_question_is_bounded_and_current_question_takes_priority(client, monkeypatch):
    add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    long_question = 'Nebula leave ' + 'x' * 900
    assert client.post(f'/api/conversations/{conversation_id}/messages', json={'question': long_question}).status_code == 201
    original = conversations.answer_question
    used = []

    def observe(*args, **kwargs):
        used.append(kwargs['previous_question'])
        return original(*args, **kwargs)

    monkeypatch.setattr(conversations, 'answer_question', observe)
    assert client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'What about it?'}).status_code == 201
    assert used == [long_question[:500]]
    assert client.post(f'/api/conversations/{conversation_id}/messages', json={'question': 'z' * 2000}).status_code == 201
    assert used[-1] == ''


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


def test_concurrent_messages_reject_stale_head(client, monkeypatch):
    add_staff_document(client)
    login(client, 'employee')
    conversation_id = chat(client)
    original = conversations.answer_question
    both_started = Barrier(2, timeout=30)

    def synchronized(*args, **kwargs):
        both_started.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(conversations, 'answer_question', synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ask(client, conversation_id).status_code, range(2)))
    assert sorted(results) == [201, 409]
    assert len(client.get(f'/api/conversations/{conversation_id}').json()['messages']) == 1
