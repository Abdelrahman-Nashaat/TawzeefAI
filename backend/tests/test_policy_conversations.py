from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': role + '-demo-only'}).status_code == 200


def add_document(client, title, text, *, audience='staff', collection='handbook'):
    response = client.post('/api/knowledge/documents/text', json={
        'title': title, 'text': text, 'audience': audience, 'collection': collection,
    })
    assert response.status_code == 201
    return response.json()['id']


def ask(client, chat_id, question, **extra):
    return client.post(f'/api/conversations/{chat_id}/messages', json={'question': question, **extra})


def test_numeric_comparison_returns_source_and_notice_in_saved_conversation(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    with TestClient(create_app(tmp_path / 'numeric-policy.sqlite3', seed_demo=True)) as client:
        login(client, 'hr')
        add_document(client, 'Remote allowance', 'Remote allowance provides 20 units monthly.')
        login(client, 'employee')
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        response = ask(client, conversation, 'Remote allowance 30 units monthly', mode='policy')
        assert response.status_code == 201
        message = response.json()
        assert message['mode'] == 'extractive'
        assert '20 units' in message['answer']
        assert 'may be a comparison or a missing condition' in message['notice']
        history = client.get(f'/api/conversations/{conversation}')
        assert history.status_code == 200
        assert 'may be a comparison or a missing condition' in history.json()['messages'][-1]['notice']


def test_prefixed_arabic_negation_abstains_in_conversation(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'arabic-negation.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        add_document(client, '\u0633\u064a\u0627\u0633\u0629 \u0631\u0633\u0648\u0645 \u0627\u0644\u0633\u0641\u0631',
                     '\u062a\u0633\u0631\u064a \u0633\u064a\u0627\u0633\u0629 \u0631\u0633\u0648\u0645 \u0627\u0644\u0633\u0641\u0631 \u0648\u0644\u0627 \u064a\u062a\u0645 \u062a\u0639\u0648\u064a\u0636\u0647\u0627.')
        login(client, 'employee')
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        response = ask(client, conversation, '\u0631\u0633\u0648\u0645 \u0627\u0644\u0633\u0641\u0631', mode='policy')
        assert response.status_code == 201
        assert response.json()['mode'] == 'abstain'
        assert response.json()['sources'] == []
        assert 'Negation' in response.json()['notice']


def test_policy_mode_respects_collection_role_and_revoked_history(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'policy-chat.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        staff_id = add_document(client, 'Nebula leave', 'Nebula leave requires seven days advance notice.')
        add_document(client, 'HR Comet leave', 'Comet leave requires three days notice.', audience='hr')
        add_document(client, 'Legacy Stardust', 'Stardust leave requires four days notice.', collection='legacy_unclassified')
        login(client, 'employee')
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        answer = ask(client, conversation, 'Nebula leave', mode='policy')
        assert answer.status_code == 201
        assert 'seven days' in answer.json()['answer']
        assert ask(client, conversation, 'Comet leave', mode='policy').json()['mode'] == 'abstain'
        assert ask(client, conversation, 'Stardust leave', mode='policy').json()['mode'] == 'abstain'
        # Old clients retain the broader general-search contract.
        assert 'four days' in ask(client, conversation, 'Stardust leave').json()['answer']
        with connect(path) as db:
            # Remains visible to this role generally, but is no longer policy.
            db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (staff_id,))
        messages = client.get(f'/api/conversations/{conversation}').json()['messages']
        assert messages[0]['mode'] == 'unavailable'
        assert 'seven days' not in messages[0]['answer']
        assert messages[-1]['mode'] != 'unavailable'


def test_policy_source_revoked_during_agent_call_fails_before_save(tmp_path, monkeypatch):
    from app import policy_agent

    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'revoked-policy.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        document_id = add_document(client, 'Nebula leave', 'Nebula leave requires seven days advance notice.')
        login(client, 'employee')
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        real = policy_agent.run_policy_agent

        def change_after_answer(*args, **kwargs):
            result = real(*args, **kwargs)
            with connect(path) as db:
                db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (document_id,))
            return result

        monkeypatch.setattr(policy_agent, 'run_policy_agent', change_after_answer)
        result = ask(client, conversation, 'Nebula leave', mode='policy')
        assert result.status_code == 409
        assert client.get(f'/api/conversations/{conversation}').json()['messages'] == []


def test_policy_mode_rejects_other_conversation_owner_and_unknown_mode(tmp_path):
    with TestClient(create_app(tmp_path / 'ownership.sqlite3', seed_demo=True)) as client:
        login(client, 'employee')
        conversation = client.post('/api/conversations', json={'title': 'Private'}).json()['id']
        assert ask(client, conversation, 'Leave', mode='not-supported').status_code == 422
        login(client, 'candidate')
        assert ask(client, conversation, 'Leave', mode='policy').status_code == 404


def test_candidate_cannot_elevate_policy_role_in_payload(tmp_path):
    with TestClient(create_app(tmp_path / 'roles.sqlite3', seed_demo=True)) as client:
        login(client, 'hr')
        add_document(client, 'Nebula leave', 'Nebula leave requires seven days advance notice.', audience='staff')
        login(client, 'candidate')
        conversation = client.post('/api/conversations', json={'title': 'Candidate'}).json()['id']
        response = ask(client, conversation, 'Nebula leave', mode='policy', role='hr')
        assert response.status_code == 201
        assert response.json()['mode'] == 'abstain'
        assert response.json()['sources'] == []
