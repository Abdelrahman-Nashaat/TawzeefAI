"""Supervised reservation for an already advanced application."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_postgres_durable_graph import create_application, slot_proposal
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_resumable_private_slot_requires_review_and_candidate_booking(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as other, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(other, 'candidate2')
        login(hr, 'hr')
        assert hr.post(f'/api/applications/{app_id}/screen').status_code == 200
        assert hr.post(f'/api/applications/{app_id}/decision', json={'decision': 'advance'}).status_code == 200
        proposal = {'intent': 'reserve_slot', 'slot': slot_proposal()}
        url = f'/api/applications/{app_id}/graph-review-preparations'
        assert candidate.post(url, json=proposal).status_code == 403
        assert hr.post(url, json={'intent': 'reserve_slot'}).status_code == 422
        response = hr.post(url, json=proposal)
        assert response.status_code == 201, response.text
        draft = response.json()['id']
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 409
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        opened = hr.get(f'/api/graph-review-drafts/{draft}').json()
        assert opened['intent'] == 'reserve_slot' and opened['slot']['interviewer'] == 'Example'
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
        result = hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True})
        assert result.status_code == 200, result.text
        assert 'must book' in result.json()['notice']
        slots = candidate.get('/api/slots', params={'application_id': app_id}).json()
        assert len(slots) == 1
        assert other.post(f"/api/slots/{slots[0]['id']}/book", json={'application_id': app_id}).status_code == 404
        assert candidate.post(f"/api/slots/{slots[0]['id']}/book", json={'application_id': app_id}).status_code == 201
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'advanced'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='decision'").fetchone()['n'] == 1


def test_reservation_stale_conflict_race_and_discard(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assert hr.post(f'/api/applications/{app_id}/screen').status_code == 200
        assert hr.post(f'/api/applications/{app_id}/decision', json={'decision': 'advance'}).status_code == 200
        payload = {'intent': 'reserve_slot', 'slot': slot_proposal()}
        url = f'/api/applications/{app_id}/graph-review-preparations'
        first = hr.post(url, json=payload).json()['id']
        second = hr.post(url, json=payload).json()['id']
        assert hr.post(f'/api/graph-review-drafts/{first}/prepare-all').status_code == 200
        assert hr.post(f'/api/graph-review-drafts/{second}/prepare-all').status_code == 200
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(hr.post, f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}) for draft in (first, second)]
            assert sorted(f.result(timeout=30).status_code for f in futures) == [200, 409]
        assert hr.post(f'/api/graph-review-drafts/{second}/resume', json={'reviewed': False}).status_code in (200, 409)
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 1


def test_existing_review_constraint_is_migrated_on_restart(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'hr')
    with db_for(mode) as db:
        db.execute('ALTER TABLE graph_review_drafts DROP CONSTRAINT graph_review_drafts_intent_check')
        db.execute("ALTER TABLE graph_review_drafts ADD CONSTRAINT graph_review_drafts_intent_check CHECK (intent IN ('review','advance'))")
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assert hr.post(f'/api/applications/{app_id}/screen').status_code == 200
        assert hr.post(f'/api/applications/{app_id}/decision', json={'decision': 'advance'}).status_code == 200
        response = hr.post(f'/api/applications/{app_id}/graph-review-preparations',
                           json={'intent': 'reserve_slot', 'slot': slot_proposal()})
        assert response.status_code == 201, response.text


def test_slot_audit_failure_rolls_back_approval(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assert hr.post(f'/api/applications/{app_id}/screen').status_code == 200
        assert hr.post(f'/api/applications/{app_id}/decision', json={'decision': 'advance'}).status_code == 200
        draft = hr.post(f'/api/applications/{app_id}/graph-review-preparations',
                        json={'intent': 'reserve_slot', 'slot': slot_proposal()}).json()['id']
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        with db_for(mode) as db:
            db.execute("ALTER TABLE slot_audit_events ADD CONSTRAINT deny_private CHECK (action <> 'created_private')")
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft,)).fetchone()['status'] == 'pending'
            db.execute('ALTER TABLE slot_audit_events DROP CONSTRAINT deny_private')
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 200
