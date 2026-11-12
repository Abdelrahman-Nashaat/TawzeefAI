"""Durable supervised reviews over a real PostgreSQL application."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import wait_for_lock

from app.main import create_app


def create_application(candidate):
    login(candidate, 'candidate')
    job = candidate.get('/api/jobs').json()[0]['id']
    response = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
    assert response.status_code == 201
    return response.json()['id']


def test_draft_persists_and_acknowledges_without_decision(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assert candidate.post(f'/api/applications/{app_id}/graph-review-drafts', json={}).status_code == 403
        response = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={'policy_question': 'leave policy'})
        assert response.status_code == 201, response.text
        draft_id = response.json()['id']
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        assert hr.get(f'/api/applications/{app_id}/graph-review-drafts').json()[0]['id'] == draft_id
        opened = hr.get(f'/api/graph-review-drafts/{draft_id}')
        assert opened.status_code == 200 and not opened.json()['stale']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).json()['status'] == 'reviewed'
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 409
    with db_for(mode) as db:
        assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
        assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action='graph_review_reviewed'", (app_id,)).fetchone()['n'] == 1


def test_changed_source_blocks_acknowledgment_but_allows_discard(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={}).json()['id']
        with db_for(mode) as db:
            db.execute("UPDATE applications SET resume_text=resume_text || ' changed' WHERE id=%s", (app_id,))
        assert hr.get(f'/api/graph-review-drafts/{draft_id}').json()['stale'] is True
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 409
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': False}).json()['status'] == 'discarded'


def test_expiry_during_lock_wait_rolls_back_resume(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={}).json()['id']
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft_id,)).fetchone()['status'] == 'pending'


def test_advance_requires_current_review_and_explicit_hr_approval(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        url = f'/api/applications/{app_id}/graph-review-drafts'
        assert hr.post(url, json={'intent': 'advance'}).status_code == 409
        source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
        start = next(i for i, char in enumerate(source['resume_text']) if not char.isspace())
        categories = ('mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education')
        items = [{'category': category, 'score': None} for category in categories]
        items[0] = {'category': categories[0], 'score': 75, 'start': start, 'end': start + 1}
        assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).status_code == 200
        proposed = hr.post(url, json={'intent': 'advance'})
        assert proposed.status_code == 201, proposed.text
        draft_id = proposed.json()['id']
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
        assert hr.get(f'/api/graph-review-drafts/{draft_id}').json()['intent'] == 'advance'
        assert candidate.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 403
        approved = hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True})
        assert approved.status_code == 200, approved.text
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'advanced'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action='decision'", (app_id,)).fetchone()['n'] == 1


def test_new_review_or_policy_revocation_invalidates_advance(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
        start = next(i for i, char in enumerate(source['resume_text']) if not char.isspace())
        categories = ('mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education')
        items = [{'category': category, 'score': None} for category in categories]
        items[0] = {'category': categories[0], 'score': 75, 'start': start, 'end': start + 1}
        assessment = {'source_digest': source['source_digest'], 'items': items}
        assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json=assessment).status_code == 200
        document = hr.post('/api/knowledge/documents/text', json={'title': 'Annual leave policy',
            'text': 'Annual leave has twelve days.', 'audience': 'hr', 'collection': 'handbook', 'page': 2}).json()['id']
        url = f'/api/applications/{app_id}/graph-review-drafts'
        first = hr.post(url, json={'intent': 'advance', 'policy_question': 'annual leave'}).json()['id']
        assert hr.post(f'/api/knowledge/documents/{document}/deactivate').status_code == 200
        assert hr.post(f'/api/graph-review-drafts/{first}/resume', json={'reviewed': True}).status_code == 409
        second = hr.post(url, json={'intent': 'advance'}).json()['id']
        assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json=assessment).status_code == 200
        assert hr.post(f'/api/graph-review-drafts/{second}/resume', json={'reviewed': True}).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action='decision'", (app_id,)).fetchone()['n'] == 0
