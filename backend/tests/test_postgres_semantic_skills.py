"""Local skill leads remain HR-only and bound to the source at inference."""
import os

import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_skill_suggestions_are_local_masked_and_read_only(mode, monkeypatch):  # noqa: F811
    import app.semantic_screening as screening

    calls = []
    def suggest(source, criteria):
        calls.append((source, criteria))
        return [{**criterion, 'candidates': []} for criterion in criteria]
    monkeypatch.setattr(screening, 'suggest', suggest)
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        with db_for(mode) as db:
            db.execute('UPDATE jobs SET required_skills=%s,preferred_skills=%s WHERE id=%s',
                       ('["Python"]', '["Rust"]', job))
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).json()['id']
        with db_for(mode) as db:
            db.execute('UPDATE applications SET resume_text=%s WHERE id=%s', ('Name: Private Person\nSkills: Python', app_id))
        url = f'/api/applications/{app_id}/semantic-skill-suggestions'
        assert candidate.post(url, json={'confirmed': True}).status_code == 403
        assert hr.post(url, json={'confirmed': False}).status_code == 422
        result = hr.post(url, json={'confirmed': True})
        assert result.status_code == 200, result.text
        assert 'Private Person' not in calls[0][0]
        assert calls[0][1] == [{'category': 'mandatory_skills', 'label': 'Python'},
                               {'category': 'preferred_skills', 'label': 'Rust'}]
        assert result.json()['criteria'][0]['label'] == 'Python'
        assert 'do not prove' in result.json()['notice']
        with db_for(mode) as db:
            row = db.execute('SELECT status,score_json FROM applications WHERE id=%s', (app_id,)).fetchone()
            assert row['status'] == 'submitted' and row['score_json'] is None
            assert db.execute('SELECT count(*) AS n FROM hr_rubric_history WHERE application_id=%s', (app_id,)).fetchone()['n'] == 0


def test_changed_job_and_revoked_session_prevent_results(mode, monkeypatch):  # noqa: F811
    import app.semantic_screening as screening

    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).json()['id']
        url = f'/api/applications/{app_id}/semantic-skill-suggestions'
        def change_job(_source, criteria):
            with db_for(mode) as db:
                db.execute('UPDATE jobs SET title=%s WHERE id=%s', ('Updated synthetic job', job))
            return criteria
        monkeypatch.setattr(screening, 'suggest', change_job)
        assert hr.post(url, json={'confirmed': True}).status_code == 409
        def revoke(_source, criteria):
            with db_for(mode) as db:
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
            return criteria
        monkeypatch.setattr(screening, 'suggest', revoke)
        assert hr.post(url, json={'confirmed': True}).status_code == 401


def test_restored_source_and_account_changes_are_rejected(mode, monkeypatch):  # noqa: F811
    import app.semantic_screening as screening

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).json()['id']
        url = f'/api/applications/{app_id}/semantic-skill-suggestions'
        def change_and_restore_source(_source, criteria):
            with db_for(mode) as db:
                original = db.execute('SELECT resume_text FROM applications WHERE id=%s', (app_id,)).fetchone()['resume_text']
                db.execute('UPDATE applications SET resume_text=%s WHERE id=%s', ('temporary', app_id))
                db.execute('UPDATE applications SET resume_text=%s WHERE id=%s', (original, app_id))
            return criteria
        monkeypatch.setattr(screening, 'suggest', change_and_restore_source)
        assert hr.post(url, json={'confirmed': True}).status_code == 409
        def change_and_restore_job(_source, criteria):
            with db_for(mode) as db:
                original = db.execute('SELECT title FROM jobs WHERE id=%s', (job,)).fetchone()['title']
                db.execute('UPDATE jobs SET title=%s WHERE id=%s', ('temporary', job))
                db.execute('UPDATE jobs SET title=%s WHERE id=%s', (original, job))
            return criteria
        monkeypatch.setattr(screening, 'suggest', change_and_restore_job)
        assert hr.post(url, json={'confirmed': True}).status_code == 409
        def change_and_restore_user(_source, criteria):
            with db_for(mode) as db:
                db.execute("UPDATE users SET active=0 WHERE username='hr'")
                db.execute("UPDATE users SET active=1 WHERE username='hr'")
            return criteria
        monkeypatch.setattr(screening, 'suggest', change_and_restore_user)
        assert hr.post(url, json={'confirmed': True}).status_code == 409


def test_unexpected_inference_error_does_not_disclose_internal_details(mode, monkeypatch):  # noqa: F811
    import app.semantic_screening as screening

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).json()['id']
        def fail(_source, _criteria):
            raise RuntimeError('sensitive internal detail')
        monkeypatch.setattr(screening, 'suggest', fail)
        response = hr.post(f'/api/applications/{app_id}/semantic-skill-suggestions', json={'confirmed': True})
        assert response.status_code == 503
        assert 'sensitive internal detail' not in response.text


def test_pinned_local_model_smoke(mode, monkeypatch):  # noqa: F811
    model_dir = os.environ.get('TAWZEEF_SEMANTIC_SMOKE_DIR')
    if not model_dir:
        pytest.skip('Pinned local semantic model not configured for smoke test')
    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', model_dir)
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        with db_for(mode) as db:
            db.execute('UPDATE jobs SET required_skills=%s,preferred_skills=%s WHERE id=%s', ('["Python"]', '[]', job))
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).json()['id']
        with db_for(mode) as db:
            db.execute('UPDATE applications SET resume_text=%s WHERE id=%s', ('Name: Synthetic Person\nSkills: Python', app_id))
        result = hr.post(f'/api/applications/{app_id}/semantic-skill-suggestions', json={'confirmed': True})
        assert result.status_code == 200, result.text
        matching = result.json()['criteria'][0]
        assert matching['label'] == 'Python'
        assert any('Python' in item['excerpt'] for item in matching['candidates'])
        assert 'Synthetic Person' not in str(matching)
