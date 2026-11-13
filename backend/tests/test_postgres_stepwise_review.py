"""Real PostgreSQL checkpoint and supervised review recovery."""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_postgres_durable_graph import assess, create_application, slot_proposal
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_stage_resume_after_failure_and_restart_then_approved_advance(mode, monkeypatch):  # noqa: F811
    import app.postgres_graph_review as graph_review

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assess(hr, app_id)
        response = hr.post(f'/api/applications/{app_id}/graph-review-preparations',
                           json={'intent': 'advance', 'slot': slot_proposal()})
        assert response.status_code == 201, response.text
        draft_id = response.json()['id']
        assert response.json()['preparation']['completed'] == ['parser']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 409
        for expected in ('matching', 'status', 'scheduling'):
            response = hr.post(f'/api/graph-review-drafts/{draft_id}/prepare')
            assert response.status_code == 200, response.text
            assert response.json()['preparation']['completed'][-1] == expected
        original = graph_review._suggested_step
        def fail(_state):
            raise RuntimeError('Synthetic interruption')
        monkeypatch.setattr(graph_review, '_suggested_step', fail)
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare').status_code == 500
        monkeypatch.setattr(graph_review, '_suggested_step', original)
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0

    # A fresh process sees the checkpoint before policy. Each HTTP request still
    # recomputes the full source snapshot for safety before proceeding.
    command = '''import os, psycopg
from psycopg import sql
from psycopg.rows import dict_row
from app.postgres_graph_review import _approval_config, _approval_graph
with psycopg.connect(os.environ['TAWZEEF_POSTGRES_DSN'], row_factory=dict_row) as db:
    db.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(os.environ['TAWZEEF_POSTGRES_SCHEMA'])))
    draft = db.execute('SELECT * FROM graph_review_drafts WHERE id=%s', (int(os.environ['CHECK_DRAFT_ID']),)).fetchone()
    state = _approval_graph(db, draft).get_state(_approval_config(draft['id']))
    assert state.next == ('policy',)
    assert all(name + '_digest' in state.values for name in ('parser','matching','status','scheduling'))
'''
    monkeypatch.setenv('CHECK_DRAFT_ID', str(draft_id))
    checked = subprocess.run([sys.executable, '-c', command], check=False, capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        opened = hr.get(f'/api/graph-review-drafts/{draft_id}').json()
        assert opened['preparation']['next'] == 'policy'
        assert opened['preparation']['completed'] == ['parser', 'matching', 'status', 'scheduling']
        response = hr.post(f'/api/graph-review-drafts/{draft_id}/prepare')
        assert response.status_code == 200, response.text
        assert response.json()['preparation']['ready'] is True
        assert response.json()['preparation']['suggested_step'] == 'review_advance'
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare').status_code == 409
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 200
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 1


def test_stale_preparation_refuses_progress_and_allows_discard(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-preparations', json={}).json()['id']
        with db_for(mode) as db:
            db.execute("UPDATE applications SET resume_text = resume_text || ' changed' WHERE id=%s", (app_id,))
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare').status_code == 409
        assert hr.get(f'/api/graph-review-drafts/{draft_id}').json()['stale'] is True
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': False}).status_code == 200


def test_preparation_enforces_roles_and_expiry_rolls_back_stage(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_experimental as pg_app
    import app.postgres_graph_review as graph_review

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assert candidate.post(f'/api/applications/{app_id}/graph-review-preparations', json={}).status_code == 403
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-preparations', json={}).json()['id']
        assert candidate.post(f'/api/graph-review-drafts/{draft_id}/prepare').status_code == 403
        original = graph_review._advance_preparation
        def expire(*args, **kwargs):
            progress = original(*args, **kwargs)
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 86400))
            return progress
        monkeypatch.setattr(graph_review, '_advance_preparation', expire)
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/prepare').status_code == 401
        monkeypatch.setattr(pg_app, 'time', time)
        monkeypatch.setattr(graph_review, '_advance_preparation', original)
        opened = hr.get(f'/api/graph-review-drafts/{draft_id}').json()
        assert opened['preparation']['completed'] == ['parser']
        assert opened['preparation']['next'] == 'matching'


def test_concurrent_preparations_serialize_each_stage(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-preparations', json={}).json()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(hr.post, f'/api/graph-review-drafts/{draft_id}/prepare') for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        assert all(response.status_code == 200 for response in results)
        assert sorted(len(response.json()['preparation']['completed']) for response in results) == [2, 3]
        progress = hr.get(f'/api/graph-review-drafts/{draft_id}').json()['preparation']
        assert progress['completed'] == ['parser', 'matching', 'status']
        assert progress['next'] == 'scheduling'


def test_prepare_all_resumes_after_partial_failure_without_decision(mode, monkeypatch):  # noqa: F811
    import app.postgres_graph_review as graph_review

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assess(hr, app_id)
        draft = hr.post(f'/api/applications/{app_id}/graph-review-preparations',
                        json={'intent': 'advance', 'slot': slot_proposal()}).json()['id']
        assert candidate.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 403
        original = graph_review._advance_preparation
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError('Synthetic interruption')
            return original(*args, **kwargs)
        monkeypatch.setattr(graph_review, '_advance_preparation', interrupted)
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 500
        monkeypatch.setattr(graph_review, '_advance_preparation', original)
        state = hr.get(f'/api/graph-review-drafts/{draft}').json()['preparation']
        assert state['completed'] == ['parser', 'matching', 'status']
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
        resumed = hr.post(f'/api/graph-review-drafts/{draft}/prepare-all')
        assert resumed.status_code == 200 and resumed.json()['preparation']['ready'] is True
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').json()['preparation']['ready'] is True
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 200


def test_concurrent_prepare_all_is_idempotent(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft = hr.post(f'/api/applications/{app_id}/graph-review-preparations', json={}).json()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(hr.post, f'/api/graph-review-drafts/{draft}/prepare-all') for _ in range(2)]
            results = [request.result(timeout=60) for request in requests]
        assert all(result.status_code == 200 and result.json()['preparation']['ready'] for result in results)
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
