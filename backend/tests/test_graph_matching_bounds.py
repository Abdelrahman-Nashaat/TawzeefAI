"""Bounded graph matching does not hold database locks or expose stale results."""

import json

import pytest
from fastapi.testclient import TestClient

from app import graph_orchestrator
from app.db import connect
from app.main import create_app


@pytest.fixture
def graph_app(tmp_path):
    path = tmp_path / 'bounded-graph.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'Built Python applications.')""", (candidate,)).lastrowid
        yield client, path, application


def test_graph_rejects_large_resume_or_job_without_scoring(graph_app, monkeypatch):
    client, path, application = graph_app
    def unexpected(*args):
        raise AssertionError('oversized inputs must not be scored')
    monkeypatch.setattr(graph_orchestrator, 'score_masked_resume', unexpected)
    for source in ('x' * 4001, '\x00' + 'x' * 20000):
        with connect(path) as db:
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', (source, application))
        response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'screen_application'})
        assert response.status_code == 422
        assert response.json()['detail']['code'] == 'graph_matching_work_limit'
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Python', application))
        db.execute('UPDATE jobs SET required_skills=?,preferred_skills=? WHERE id=?',
                   (json.dumps([f'skill{i}' for i in range(21)]), '[]', 'job-backend-python'))
    assert client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'screen_application'}).status_code == 422
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['resume', 'job', 'session'])
def test_graph_rechecks_inputs_and_hr_after_scoring_without_write_lock(graph_app, monkeypatch, change):
    client, path, application = graph_app
    original = graph_orchestrator.score_masked_resume
    def concurrent_change(job, source):
        # This write succeeds while scoring, proving the graph holds no SQLite read transaction here.
        with connect(path) as db:
            if change == 'resume':
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('temporary', application))
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Built Python applications.', application))
            elif change == 'job':
                row = db.execute('SELECT required_skills FROM jobs WHERE id=?', ('job-backend-python',)).fetchone()
                db.execute('UPDATE jobs SET required_skills=? WHERE id=?', ('["Temporary"]', 'job-backend-python'))
                db.execute('UPDATE jobs SET required_skills=? WHERE id=?', (row[0], 'job-backend-python'))
            else:
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
        return original(job, source)
    monkeypatch.setattr(graph_orchestrator, 'score_masked_resume', concurrent_change)
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'screen_application'})
    assert response.status_code == (401 if change == 'session' else 409)
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0] == 0
        assert db.execute('SELECT score_json FROM applications WHERE id=?', (application,)).fetchone()[0] is None


def test_graph_scores_exact_limits_without_truncation(graph_app):
    client, path, application = graph_app
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('x' * 3993 + ' Python', application))
        db.execute('UPDATE jobs SET required_skills=?,preferred_skills=? WHERE id=?',
                   (json.dumps(['Python'] + [f'skill{i}' for i in range(19)]), '[]', 'job-backend-python'))
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'screen_application'})
    assert response.status_code == 201
    matching = response.json()['state']['matching']
    assert len(matching['required']) == 20
    assert matching['required'][0] == {'skill': 'Python', 'mentioned': True}
    assert response.json()['awaiting_review']


def test_full_review_survives_work_limit_without_scoring_and_resume(graph_app, monkeypatch):
    client, path, application = graph_app
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('x' * 4001, application))
    def unexpected(*args):
        raise AssertionError('oversized inputs must not be scored')
    monkeypatch.setattr(graph_orchestrator, 'score_masked_resume', unexpected)
    response = client.post('/api/graph-reviews', json={'application_id': application})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['awaiting_review']
    assert result['state']['matching_unavailable']['status'] == 'limit_exceeded'
    assert 'matching' not in result['state'] and 'matching_specialist' not in result['state']
    assert 'matching_unavailable_ref' not in result['state']
    saved = client.get(f"/api/graph-reviews/{result['id']}")
    assert saved.json()['state']['matching_unavailable']['status'] == 'limit_exceeded'
    resumed = client.post(f"/api/graph-reviews/{result['id']}/resume", json={'reviewed': True})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()['state']['matching_unavailable']['status'] == 'limit_exceeded'
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action IN ('screened','decision')", (application,)).fetchone()[0] == 0


@pytest.mark.parametrize('change', ['source', 'job'])
def test_unavailable_marker_becomes_stale_after_change_and_revert(graph_app, change):
    client, path, application = graph_app
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('x' * 4001, application))
    created = client.post('/api/graph-reviews', json={'application_id': application})
    assert created.status_code == 201, created.text
    review_id = created.json()['id']
    with connect(path) as db:
        if change == 'source':
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('temporary', application))
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('x' * 4001, application))
        else:
            job = db.execute("SELECT title FROM jobs WHERE id='job-backend-python'").fetchone()[0]
            db.execute("UPDATE jobs SET title='temporary' WHERE id='job-backend-python'")
            db.execute("UPDATE jobs SET title=? WHERE id='job-backend-python'", (job,))
    result = client.get(f'/api/graph-reviews/{review_id}')
    assert result.status_code == 200
    assert result.json()['state']['matching_unavailable']['status'] == 'stale'
    assert 'matching' not in result.json()['state']
