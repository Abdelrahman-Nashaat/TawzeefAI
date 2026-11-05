import pytest
from fastapi.testclient import TestClient

from app import adaptive_planner, graph_orchestrator
from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'adaptive.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            def insert(status, score):
                return db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status,score_json)
                    VALUES (?, 'job-backend-python', 'synthetic.docx', 'Built Python', ?, ?)""",
                    (candidate, status, score)).lastrowid
            unscreened = insert('submitted', None)
            advanced = insert('advanced', '{}')
        yield path, client, unscreened, advanced


def start(client, application_id, **extra):
    return client.post('/api/graph-reviews', json={
        'application_id': application_id, 'intent': 'adaptive', 'adaptive_goal': 'review_application', **extra})


def test_dynamic_plan_checkpoint_resume_and_stale_guard(setup):
    path, client, unscreened, advanced = setup
    first = start(client, unscreened)
    second = start(client, advanced)
    assert first.status_code == second.status_code == 201
    a, b = first.json(), second.json()
    assert a['state']['plan'] == ['parser', 'matching', 'status', 'human_review']
    assert b['state']['plan'] == ['status', 'scheduler', 'human_review']
    assert a['state']['plan_source'] == b['state']['plan_source'] == 'deterministic'
    assert 'Built Python' not in str(a)
    assert 'context_digest' not in a['state']
    reopened = client.get(f"/api/graph-reviews/{a['id']}")
    assert reopened.status_code == 200 and reopened.json()['awaiting_review']
    resumed = client.post(f"/api/graph-reviews/{a['id']}/resume", json={'reviewed': True})
    assert resumed.status_code == 200 and not resumed.json()['awaiting_review']
    assert client.post(f"/api/graph-reviews/{a['id']}/resume", json={'reviewed': True}).status_code == 409
    with connect(path) as db:
        db.execute("UPDATE applications SET resume_text='Changed' WHERE id=?", (advanced,))
    assert client.get(f"/api/graph-reviews/{b['id']}").json()['state']['plan_stale']
    assert client.post(f"/api/graph-reviews/{b['id']}/resume", json={'reviewed': True}).status_code == 409
    assert client.get(f"/api/graph-reviews/{b['id']}").json()['awaiting_review']
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_screened_submitted_application_still_gets_matching_review(setup):
    path, client, application, _ = setup
    assert client.post(f'/api/applications/{application}/screen').status_code == 200
    response = start(client, application)
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['plan'] == ['parser', 'matching', 'status', 'human_review']
    assert review['state']['matching']['required']
    assert review['awaiting_review']
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True}).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_model_cannot_omit_matching_from_screened_submitted_review():
    with pytest.raises(ValueError):
        adaptive_planner.validate_steps({'steps': ['status']}, {'status': 'submitted', 'has_screening': True})


@pytest.mark.parametrize('change', ['resume', 'role'])
def test_screened_review_rechecks_changes_during_matching(setup, monkeypatch, change):
    path, client, application, _ = setup
    assert client.post(f'/api/applications/{application}/screen').status_code == 200
    original = graph_orchestrator.score_masked_resume

    def change_during_matching(job, source):
        result = original(job, source)
        with connect(path) as db:
            if change == 'resume':
                db.execute("UPDATE applications SET resume_text='Changed' WHERE id=?", (application,))
            else:
                db.execute("UPDATE users SET role='employee' WHERE username='hr'")
        return result

    monkeypatch.setattr(graph_orchestrator, 'score_masked_resume', change_during_matching)
    response = start(client, application)
    assert response.status_code == (409 if change == 'resume' else 403), response.text


def test_model_plan_is_bounded_and_invalid_output_falls_back(setup, monkeypatch):
    _, client, _, advanced = setup
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'fake-key')
    captured = []
    def valid(facts, key):
        captured.append(facts)
        return {'steps': ['matching', 'status', 'scheduler']}
    monkeypatch.setattr(adaptive_planner, '_request_model', valid)
    result = start(client, advanced)
    assert result.status_code == 201
    assert result.json()['state']['plan_source'] == 'model'
    assert result.json()['state']['plan'] == ['matching', 'status', 'scheduler', 'human_review']
    assert captured == [{'status': 'advanced', 'has_screening': True}]
    monkeypatch.setattr(adaptive_planner, '_request_model', lambda *_: {
        'steps': ['status', 'book_interview'], 'application_id': 999})
    result = start(client, advanced)
    assert result.status_code == 201
    assert result.json()['state']['plan_source'] == 'deterministic_fallback'
    assert result.json()['state']['plan'] == ['status', 'scheduler', 'human_review']
    def unavailable(*_):
        raise TimeoutError('provider unavailable')
    monkeypatch.setattr(adaptive_planner, '_request_model', unavailable)
    result = start(client, advanced)
    assert result.status_code == 201
    assert result.json()['state']['plan_source'] == 'deterministic_fallback'


def test_untrusted_plan_and_identity_rejected_and_hr_revocation_blocks_resume(setup):
    path, client, unscreened, _ = setup
    graph_orchestrator.initialize_graph_reviews(path)
    with connect(path) as db:
        before = db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0]
    assert start(client, unscreened, steps=['book_interview']).status_code == 422
    assert start(client, unscreened, candidate_id=999).status_code == 422
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0] == before
    review = start(client, unscreened).json()
    with connect(path) as db:
        db.execute("UPDATE users SET role='employee' WHERE username='hr'")
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True}).status_code == 403


@pytest.mark.parametrize('proposal', [
    {'steps': ['matching', 'parser', 'status']},
    {'steps': ['parser', 'matching', 'status', 'status']},
    {'steps': ['parser', 'matching', 'scheduler', 'status']},
    {'steps': ['parser', 'matching', 'status'], 'application_id': 3},
])
def test_invalid_plans_are_rejected(proposal):
    with pytest.raises(ValueError):
        adaptive_planner.validate_steps(proposal, {'status': 'submitted', 'has_screening': False})
