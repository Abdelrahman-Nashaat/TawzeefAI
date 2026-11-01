from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app import policy_agent
from app.db import connect
from app.graph_orchestrator import _config, build_review_graph
from app.main import create_app


def test_graph_calls_policy_agent_and_rechecks_policy_scope(tmp_path, monkeypatch):
    path = tmp_path / 'graph-policy.sqlite3'
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    calls = []

    def plan(question, key):
        calls.append('search')
        return {'tool_calls': [{'type': 'function', 'function': {
            'name': 'search_policy', 'arguments': '{"query":"quasar leave"}'}}]}

    def generate(question, passages):
        calls.append('generate')
        return f"model-answer-marker [{passages[0]['id']}]"

    monkeypatch.setattr(policy_agent, '_request_search', plan)
    monkeypatch.setattr(policy_agent, 'generate_with_groq', generate)
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','test.pdf','synthetic')""", (candidate,)).lastrowid
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Quasar leave', 'audience': 'hr', 'collection': 'handbook',
            'text': 'Quasar leave is eleven days. confidential-marker',
        }).json()
        question = 'What is quasar leave?'
        response = client.post('/api/graph-reviews', json={
            'application_id': application, 'intent': 'policy', 'policy_question': question,
        })
        assert response.status_code == 201, response.text
        review = response.json()
        assert calls == ['search', 'generate']
        assert 'confidential-marker' in review['state']['policy']['answer']
        assert 'model-answer-marker' not in response.text
        with SqliteSaver.from_conn_string(str(path)) as saver:
            history = list(build_review_graph(path, saver).get_state_history(_config(review['id'])))
            assert question not in str(history)
            assert 'confidential-marker' not in str(history)
            assert 'model-answer-marker' not in str(history)
        # A formerly authorized source may remain generally visible but cease
        # to be a policy. Reloads must then hide the old extract completely.
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (doc['id'],))
        restored = client.get(f"/api/graph-reviews/{review['id']}")
        assert restored.status_code == 200
        assert restored.json()['state']['policy'] == {'status': 'unavailable'}
        assert 'confidential-marker' not in restored.text


def test_graph_policy_does_not_treat_job_descriptions_as_policy(tmp_path):
    path = tmp_path / 'graph-scope.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','test.pdf','synthetic')""", (candidate,)).lastrowid
        response = client.post('/api/graph-reviews', json={
            'application_id': application, 'intent': 'policy', 'policy_question': 'Python SQL policy',
        })
        assert response.status_code == 201
        assert response.json()['state']['policy']['status'] == 'abstain'


def test_abstention_with_suggested_passages_is_not_presented_as_an_answer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import graph_orchestrator

    suggestion = SimpleNamespace(id='suggestion', text='Unverified suggestion')
    monkeypatch.setattr(graph_orchestrator, 'run_policy_agent', lambda *args, **kwargs:
                        SimpleNamespace(answer=SimpleNamespace(mode='abstain', sources=[suggestion])))
    path = tmp_path / 'graph-abstention.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','test.pdf','synthetic')""", (candidate,)).lastrowid
        response = client.post('/api/graph-reviews', json={
            'application_id': application, 'intent': 'policy', 'policy_question': 'Unanswered question',
        })
        assert response.status_code == 201, response.text
        assert response.json()['state']['policy'] == {'status': 'abstain'}
        assert 'Unverified suggestion' not in response.text


def test_legacy_policy_references_remain_readable_but_respect_revocation(tmp_path):
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.auth import User
    from app.conversations import _authorized_sources, _digest
    from app.graph_orchestrator import _view

    path = tmp_path / 'legacy-policy.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login = client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
        assert login.status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','test.pdf','synthetic')""", (candidate,)).lastrowid
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Legacy manual', 'audience': 'hr', 'text': 'Legacy-marker for previous reviews.',
        }).json()
        with connect(path) as db:
            source = next(s for s in _authorized_sources(db, 'hr').values()
                          if s.id.startswith(f"db-doc-{doc['id']}-"))
        snapshot = SimpleNamespace(values={'application_id': application, 'intent': 'policy',
            'policy': {'status': 'source_refs_v1', 'refs': [{'id': source.id, 'digest': _digest(source)}]}}, next=())
        request = Request({'type': 'http', 'headers': [(b'cookie',
            f"tawzeef_session={client.cookies.get('tawzeef_session')}".encode())]})
        user = User(**{key: login.json()[key] for key in ('id', 'username', 'display_name', 'role')})
        assert 'Legacy-marker' in _view(snapshot, path, request=request, user=user)['state']['policy']['answer']
        with connect(path) as db:
            db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc['id'],))
        assert _view(snapshot, path, request=request, user=user)['state']['policy'] == {'status': 'unavailable'}
