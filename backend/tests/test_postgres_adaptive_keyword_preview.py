"""Adaptive review executes a source-bound matching preview without decisions."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_preview_is_review_only_source_bound_and_survives_restart(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        with db_for(mode) as db:
            db.execute("UPDATE jobs SET required_skills=%s,preferred_skills=%s WHERE id=%s", ('["Python", "Rust"]', '[]', job))
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).json()['id']
        endpoint = f'/api/applications/{app_id}/adaptive-review-preparations'
        assert candidate.post(endpoint, json={'keyword_preview': True}).status_code == 403
        created = hr.post(endpoint, json={'keyword_preview': True})
        assert created.status_code == 201, created.text
        draft_id = created.json()['id']
        assert 'matching' in created.json()['plan']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare-all').status_code == 200
        opened = hr.get(f'/api/graph-review-drafts/{draft_id}').json()
        matching = opened['result']['matching']
        assert matching['status'] == 'unavailable'
        assert matching['keyword_preview']['method'].startswith('Conservative keyword mentions')
        required = matching['keyword_preview']['required']
        assert [(item['skill'], item['mentioned']) for item in required] == [('Python', True), ('Rust', False)]
        assert 'Python' in required[0]['evidence'][0]
        assert 'not a human assessment' in matching['preview_notice']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 200
        with db_for(mode) as db:
            app = db.execute('SELECT status,score_json FROM applications WHERE id=%s', (app_id,)).fetchone()
            assert app['status'] == 'submitted' and app['score_json'] is None
            assert db.execute('SELECT count(*) AS n FROM hr_rubric_history WHERE application_id=%s', (app_id,)).fetchone()['n'] == 0
        stale_draft = hr.post(endpoint, json={'keyword_preview': True}).json()['id']
        with db_for(mode) as db:
            db.execute('UPDATE applications SET resume_text=%s WHERE id=%s', ('Changed synthetic CV', app_id))
        assert hr.post(f'/api/graph-review-drafts/{stale_draft}/prepare').status_code == 409
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        reopened = hr.get(f'/api/graph-review-drafts/{stale_draft}')
        assert reopened.status_code == 200 and reopened.json()['stale'] is True


def test_legacy_adaptive_review_does_not_gain_preview(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).json()['id']
        result = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations', json={})
        draft_id = result.json()['id']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare-all').status_code == 200
        matching = hr.get(f'/api/graph-review-drafts/{draft_id}').json()['result']['matching']
        assert 'keyword_preview' not in matching
        with db_for(mode) as db:
            assert db.execute('SELECT checkpoint_version FROM graph_review_drafts WHERE id=%s', (draft_id,)).fetchone()['checkpoint_version'] == 6
