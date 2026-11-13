"""Durable supervised reviews over a real PostgreSQL application."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import wait_for_lock

pytest.importorskip('langgraph.checkpoint.postgres')

from app.main import create_app
from app.postgres_graph_review import _approval_config, _approval_graph


def create_application(candidate):
    login(candidate, 'candidate')
    job = candidate.get('/api/jobs').json()[0]['id']
    response = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
    assert response.status_code == 201
    return response.json()['id']


def assess(hr, app_id):
    source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
    start = next(i for i, char in enumerate(source['resume_text']) if not char.isspace())
    categories = ('mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education')
    items = [{'category': category, 'score': None} for category in categories]
    items[0] = {'category': categories[0], 'score': 75, 'start': start, 'end': start + 1}
    assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).status_code == 200


def slot_proposal():
    start = datetime.now(UTC) + timedelta(days=2)
    return {'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Example'}


def test_approved_slot_is_private_and_requires_candidate_booking(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as other, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        login(other, 'candidate2')
        assess(hr, app_id)
        slot = slot_proposal()
        url = f'/api/applications/{app_id}/graph-review-drafts'
        assert hr.post(url, json={'slot': slot}).status_code == 422
        response = hr.post(url, json={'intent': 'advance', 'slot': slot})
        assert response.status_code == 201, response.text
        draft = response.json()['id']
        assert hr.get(f'/api/graph-review-drafts/{draft}').json()['slot']['interviewer'] == 'Example'
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 200
        slots = candidate.get('/api/slots', params={'application_id': app_id}).json()
        assert len(slots) == 1
        assert other.post(f"/api/slots/{slots[0]['id']}/book", json={'application_id': app_id}).status_code == 404
        assert candidate.post(f"/api/slots/{slots[0]['id']}/book", json={'application_id': app_id}).status_code == 201
        with db_for(mode) as db:
            assert db.execute("SELECT count(*) AS n FROM slot_audit_events WHERE action='created_private'").fetchone()['n'] == 1
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='decision'").fetchone()['n'] == 1


def test_slot_conflict_and_audit_failure_leave_application_unchanged(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assess(hr, app_id)
        slot = slot_proposal()
        draft = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={'intent': 'advance', 'slot': slot}).json()['id']
        assert hr.post('/api/slots', json=slot).status_code == 201
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 409
        with db_for(mode) as db:
            db.execute('DELETE FROM slot_audit_events')
            db.execute('DELETE FROM interview_slots')
            db.execute("ALTER TABLE slot_audit_events ADD CONSTRAINT reject_private CHECK (action <> 'created_private')")
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft,)).fetchone()['status'] == 'pending'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='decision'").fetchone()['n'] == 0


def test_slot_approval_race_and_expired_session(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assess(hr, app_id)
        payload = {'intent': 'advance', 'slot': slot_proposal()}
        url = f'/api/applications/{app_id}/graph-review-drafts'
        draft = hr.post(url, json=payload).json()['id']
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        monkeypatch.setattr(pg_app, 'time', time)
        other = hr.post(url, json=payload).json()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(hr.post, f'/api/graph-review-drafts/{item}/resume', json={'reviewed': True}) for item in (draft, other)]
            assert sorted(f.result(timeout=20).status_code for f in futures) == [200, 409]
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 1
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='decision'").fetchone()['n'] == 1


def test_draft_persists_and_acknowledges_without_decision(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assert candidate.post(f'/api/applications/{app_id}/graph-review-drafts', json={}).status_code == 403
        response = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={'policy_question': 'leave policy'})
        assert response.status_code == 201, response.text
        draft_id = response.json()['id']
    # A fresh connection can inspect the actual LangGraph interrupt, not just
    # the application-level draft. Source text and questions are not in state.
    with db_for(mode) as db:
        state = _approval_graph(db).get_state(_approval_config(draft_id))
        assert state.next == ('human_review',)
        assert len(state.interrupts) == 1
        assert set(state.values) == {'draft_id', 'fingerprint', 'intent'}
        assert state.values['draft_id'] == draft_id
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        assert hr.get(f'/api/applications/{app_id}/graph-review-drafts').json()[0]['id'] == draft_id
        opened = hr.get(f'/api/graph-review-drafts/{draft_id}')
        assert opened.status_code == 200 and not opened.json()['stale']
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).json()['status'] == 'reviewed'
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 409
    with db_for(mode) as db:
        state = _approval_graph(db).get_state(_approval_config(draft_id))
        assert not state.next and state.values['reviewed'] is True
        assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
        assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action='graph_review_reviewed'", (app_id,)).fetchone()['n'] == 1


def test_missing_checkpoint_fails_closed(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={}).json()['id']
        with db_for(mode) as db:
            db.execute('DELETE FROM checkpoints WHERE thread_id=%s', (f'graph-review-{draft_id}',))
        response = hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True})
        assert response.status_code == 409, response.text
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft_id,)).fetchone()['status'] == 'pending'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='graph_review_reviewed'").fetchone()['n'] == 0


def test_failed_commit_rolls_back_checkpoint_resume(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={}).json()['id']
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT reject_review CHECK (action <> 'graph_review_reviewed')")
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 500
        with db_for(mode) as db:
            state = _approval_graph(db).get_state(_approval_config(draft_id))
            assert state.next == ('human_review',) and len(state.interrupts) == 1
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft_id,)).fetchone()['status'] == 'pending'
            db.execute('ALTER TABLE audit_events DROP CONSTRAINT reject_review')
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 200


def test_failed_final_audit_rolls_back_advance_slot_and_checkpoint(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assess(hr, app_id)
        response = hr.post(f'/api/applications/{app_id}/graph-review-drafts',
                           json={'intent': 'advance', 'slot': slot_proposal()})
        assert response.status_code == 201, response.text
        draft_id = response.json()['id']
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT reject_review CHECK (action <> 'graph_review_reviewed')")
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 500
        with db_for(mode) as db:
            state = _approval_graph(db).get_state(_approval_config(draft_id))
            assert state.next == ('human_review',) and len(state.interrupts) == 1
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft_id,)).fetchone()['status'] == 'pending'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='decision'").fetchone()['n'] == 0
            db.execute('ALTER TABLE audit_events DROP CONSTRAINT reject_review')
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True}).status_code == 200
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 1


def test_legacy_draft_can_still_be_discarded(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        draft_id = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={}).json()['id']
        with db_for(mode) as db:
            db.execute('UPDATE graph_review_drafts SET checkpoint_version=0 WHERE id=%s', (draft_id,))
            db.execute('DELETE FROM checkpoints WHERE thread_id=%s', (f'graph-review-{draft_id}',))
        assert hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': False}).json()['status'] == 'discarded'


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


@pytest.mark.parametrize('account_change', ['inactive', 'other_role'])
def test_advance_rejects_ineligible_candidate_account(mode, account_change):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
        start = next(i for i, char in enumerate(source['resume_text']) if not char.isspace())
        categories = ('mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education')
        items = [{'category': category, 'score': None} for category in categories]
        items[0] = {'category': categories[0], 'score': 75, 'start': start, 'end': start + 1}
        assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).status_code == 200
        response = hr.post(f'/api/applications/{app_id}/graph-review-drafts', json={'intent': 'advance'})
        assert response.status_code == 201, response.text
        draft_id = response.json()['id']
        with db_for(mode) as db:
            if account_change == 'inactive':
                db.execute("UPDATE users SET active=0 WHERE username='candidate'")
            else:
                db.execute("UPDATE users SET role='employee' WHERE username='candidate'")
        response = hr.post(f'/api/graph-review-drafts/{draft_id}/resume', json={'reviewed': True})
        assert response.status_code == 409, response.text
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft_id,)).fetchone()['status'] == 'pending'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action IN ('decision', 'graph_review_reviewed')", (app_id,)).fetchone()['n'] == 0
