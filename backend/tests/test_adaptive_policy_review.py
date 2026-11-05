from langgraph.checkpoint.sqlite import SqliteSaver
from test_adaptive_planner import setup  # noqa: F401

from app import adaptive_planner, graph_orchestrator
from app.db import connect
from app.graph_orchestrator import _config, build_review_graph


def test_adaptive_review_includes_authorized_policy_without_checkpointing_text(setup):  # noqa: F811
    path, client, application, _ = setup
    question = 'What is quasar leave?'
    document = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar leave', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days. synthetic-policy-marker',
    }).json()
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'adaptive',
        'adaptive_goal': 'review_application', 'policy_question': question,
    })
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['plan'] == ['parser', 'matching', 'status', 'policy', 'human_review']
    assert review['state']['policy']['status'] == 'answer'
    assert 'synthetic-policy-marker' in review['state']['policy']['answer']
    assert review['awaiting_review']
    with SqliteSaver.from_conn_string(str(path)) as saver:
        history = list(build_review_graph(path, saver).get_state_history(_config(review['id'])))
    assert question not in str(history)
    assert 'synthetic-policy-marker' not in str(history)
    second = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'adaptive',
        'adaptive_goal': 'review_application', 'policy_question': question,
    }).json()
    resumed = client.post(f"/api/graph-reviews/{second['id']}/resume", json={'reviewed': True})
    assert resumed.status_code == 200, resumed.text
    assert not resumed.json()['awaiting_review']
    with connect(path) as db:
        db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (document['id'],))
    reopened = client.get(f"/api/graph-reviews/{review['id']}")
    assert reopened.status_code == 200
    assert reopened.json()['state']['policy'] == {'status': 'unavailable'}
    assert 'synthetic-policy-marker' not in reopened.text
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True}).status_code == 409
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': False}).status_code == 200


def test_adaptive_policy_is_hr_scoped(setup):  # noqa: F811
    _, client, application, _ = setup
    base = {'application_id': application, 'intent': 'adaptive',
            'adaptive_goal': 'review_application', 'policy_question': 'What is quasar leave?'}
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.post('/api/graph-reviews', json=base).status_code == 403


def test_policy_question_never_reaches_the_adaptive_planner(setup, monkeypatch):  # noqa: F811
    _, client, application, _ = setup
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-key')
    captured = []
    def choose(facts, key):
        captured.append(dict(facts))
        return {'steps': ['parser', 'matching', 'status']}
    monkeypatch.setattr(adaptive_planner, '_request_model', choose)
    original = graph_orchestrator.run_policy_agent
    def local_policy(*args, **kwargs):
        kwargs['enabled'] = False
        return original(*args, **kwargs)
    monkeypatch.setattr(graph_orchestrator, 'run_policy_agent', local_policy)
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'adaptive', 'adaptive_goal': 'review_application',
        'policy_question': 'private question marker',
    })
    assert response.status_code == 201, response.text
    assert captured == [{'status': 'submitted', 'has_screening': False}]
    assert 'policy' in response.json()['state']['plan']
    assert 'private question marker' not in str(captured)


def test_policy_call_fails_if_application_changes_during_lookup(setup, monkeypatch):  # noqa: F811
    path, client, application, _ = setup
    original = graph_orchestrator.run_policy_agent
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with connect(path) as db:
            db.execute("UPDATE applications SET resume_text='Changed during lookup' WHERE id=?", (application,))
        return result
    monkeypatch.setattr(graph_orchestrator, 'run_policy_agent', changed)
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'adaptive', 'adaptive_goal': 'review_application',
        'policy_question': 'What is quasar leave?',
    })
    assert response.status_code == 409, response.text


def test_policy_call_fails_if_hr_access_is_revoked_during_lookup(setup, monkeypatch):  # noqa: F811
    path, client, application, _ = setup
    original = graph_orchestrator.run_policy_agent
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        with connect(path) as db:
            db.execute("UPDATE users SET role='employee' WHERE username='hr'")
        return result
    monkeypatch.setattr(graph_orchestrator, 'run_policy_agent', revoked)
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'adaptive', 'adaptive_goal': 'review_application',
        'policy_question': 'What is quasar leave?',
    })
    assert response.status_code == 403, response.text
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0] == 0
