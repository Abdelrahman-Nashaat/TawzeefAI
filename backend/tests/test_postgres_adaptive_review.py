"""Adaptive HR plans remain bounded, resumable and source-bound."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def test_adaptive_submitted_plan_survives_restart_and_requires_review(mode, monkeypatch):  # noqa: F811
    import app.postgres_graph_review as graph

    called = []
    original = graph._specialist_nodes
    def recording(*args, **kwargs):
        result = []
        for name, specialist in original(*args, **kwargs):
            def invoke(state, name=name, specialist=specialist):
                called.append(name)
                return specialist(state)
            result.append((name, invoke))
        return result
    monkeypatch.setattr(graph, '_specialist_nodes', recording)
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        url = f'/api/applications/{app_id}/adaptive-review-preparations'
        assert candidate.post(url, json={}).status_code == 403
        response = hr.post(url, json={})
        assert response.status_code == 201, response.text
        draft = response.json()
        assert draft['plan'] == ['parser', 'matching', 'status']
        assert draft['plan_source'] == 'deterministic'
        assert draft['preparation']['completed'] == ['parser']
        assert {'parser', 'matching', 'status'} <= set(called)
        assert 'scheduling' not in called and 'policy' not in called
        draft_id = draft['id']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        resumed = hr.get(f'/api/graph-review-drafts/{draft_id}').json()
        assert resumed['plan'] == ['parser', 'matching', 'status']
        assert list(resumed['result']) == ['parsed']
        completed = hr.post(f'/api/graph-review-drafts/{draft_id}/prepare-all')
        assert completed.status_code == 200, completed.text
        assert completed.json()['preparation']['completed'] == ['parser', 'matching', 'status']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 200
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'


def test_advanced_plan_uses_availability_and_rejects_changed_sources(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, _ = ready_booking(candidate, hr)
        response = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations', json={})
        assert response.status_code == 201, response.text
        draft = response.json()
        assert draft['plan'] == ['parser', 'matching', 'status', 'scheduling']
        draft_id = draft['id']
        with db_for(mode) as db:
            db.execute("UPDATE applications SET resume_text=resume_text || ' updated' WHERE id=%s", (app_id,))
        assert hr.get(f'/api/graph-review-drafts/{draft_id}').json()['stale'] is True
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare-all').status_code == 409
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 409


def test_explicit_policy_question_adds_authorized_policy_specialist(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        doc = hr.post('/api/knowledge/documents/text', json={'title': 'Synthetic leave', 'text': 'Annual leave has twelve days.',
            'audience': 'hr', 'collection': 'handbook', 'page': 2})
        assert doc.status_code == 201
        result = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations', json={'policy_question': 'annual leave'})
        assert result.status_code == 201, result.text
        draft = result.json()
        assert draft['plan'] == ['parser', 'matching', 'status', 'policy']
        assert hr.post(f"/api/graph-review-drafts/{draft['id']}/prepare-all").status_code == 200
        review = hr.get(f"/api/graph-review-drafts/{draft['id']}").json()
        assert review['result']['policy']['sources'][0]['title'] == 'Synthetic leave'
        assert review['result']['policy']['sources'][0]['page'] == 2


def test_adaptive_slot_proposal_requires_hr_approval_and_candidate_booking(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, _ = ready_booking(candidate, hr)
        start = datetime.now(UTC) + timedelta(days=10)
        slot = {'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(),
                'interviewer': 'Adaptive Interviewer'}
        result = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations',
                         json={'intent': 'reserve_slot', 'slot': slot})
        assert result.status_code == 201, result.text
        draft_id = result.json()['id']
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots WHERE target_application_id=%s', (app_id,)).fetchone()['n'] == 0
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare-all').status_code == 200
        accepted = hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True})
        assert accepted.status_code == 200, accepted.text
        with db_for(mode) as db:
            row = db.execute('SELECT id FROM interview_slots WHERE target_application_id=%s', (app_id,)).fetchone()
            assert row is not None
            assert db.execute('SELECT count(*) AS n FROM interviews WHERE application_id=%s', (app_id,)).fetchone()['n'] == 0
            slot_id = row['id']
        booked = candidate.post(f'/api/slots/{slot_id}/book', json={'application_id': app_id})
        assert booked.status_code == 201, booked.text


def test_malicious_planner_cannot_write_or_change_identity(mode, monkeypatch):  # noqa: F811
    import app.postgres_graph_review as graph

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        monkeypatch.setattr(graph, 'make_plan', lambda facts, enabled: (['parser', 'matching', 'delete_application', 'status'], 'model'))
        result = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations', json={})
        assert result.status_code == 503
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT count(*) AS n FROM graph_review_drafts').fetchone()['n'] == 0


def test_explicit_model_plan_uses_only_status_facts_and_rejects_unsafe_output(mode, monkeypatch):  # noqa: F811
    import app.adaptive_planner as planner

    observed = []
    def unsafe_request(facts, key):
        observed.append(dict(facts))
        return {'steps': ['parser', 'matching', 'status', 'delete_application']}
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-test-key')
    monkeypatch.setattr(planner, '_request_model', unsafe_request)
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        result = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations', json={'model_plan': True})
        assert result.status_code == 201, result.text
        assert result.json()['plan'] == ['parser', 'matching', 'status']
        assert result.json()['plan_source'] == 'deterministic_fallback'
        assert observed == [{'status': 'submitted', 'has_screening': False}]


def test_source_changes_during_planning_are_rejected(mode, monkeypatch):  # noqa: F811
    import app.postgres_graph_review as graph

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        def change_source(facts, *, enabled):
            with db_for(mode) as db:
                db.execute("UPDATE applications SET resume_text=resume_text || ' changed' WHERE id=%s", (app_id,))
            return ['parser', 'matching', 'status'], 'deterministic'
        monkeypatch.setattr(graph, 'make_plan', change_source)
        result = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations', json={})
        assert result.status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM graph_review_drafts').fetchone()['n'] == 0


def test_session_expiry_during_adaptive_start_is_denied(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, _ = ready_booking(candidate, hr)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/applications/{app_id}/adaptive-review-preparations', json={})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
