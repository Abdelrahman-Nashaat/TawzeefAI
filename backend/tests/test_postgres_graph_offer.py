"""Resumable offer proposals require fresh human approval and candidate consent."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_offers import complete

from app.main import create_app


def proposal(hr, app_id, details='Synthetic terms'):
    response = hr.post(f'/api/applications/{app_id}/graph-review-preparations',
                       json={'intent': 'present_offer', 'offer_details': details})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_offer_review_requires_approval_and_candidate_response(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = complete(mode, candidate, hr)
        url = f'/api/applications/{app_id}/graph-review-preparations'
        assert candidate.post(url, json={'intent': 'present_offer', 'offer_details': 'Terms'}).status_code == 403
        assert hr.post(url, json={'intent': 'present_offer', 'offer_details': '  '}).status_code == 422
        draft = proposal(hr, app_id)
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 409
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        opened = hr.get(f'/api/graph-review-drafts/{draft}').json()
        assert opened['offer_details'] == 'Synthetic terms'
        assert candidate.get(f'/api/applications/{app_id}/offers').json() == []
        with db_for(mode) as db:
            rows = db.execute('SELECT checkpoint::text AS value FROM checkpoints WHERE thread_id=%s',
                              (f'graph-review-{draft}',)).fetchall()
            assert rows and all('Synthetic terms' not in row['value'] for row in rows)
        response = hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True})
        assert response.status_code == 200, response.text
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 409
        offer = candidate.get(f'/api/applications/{app_id}/offers').json()[0]
        assert offer['details'] == 'Synthetic terms' and offer['status'] == 'presented'
        assert candidate.post(f"/api/offers/{offer['id']}/respond", json={'decision': 'accepted'}).status_code == 200
        assert hr.post(f'/api/applications/{app_id}/decision', json={'decision': 'hire'}).status_code == 200


def test_offer_source_and_terms_changes_fail_closed(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = complete(mode, candidate, hr)
        draft = proposal(hr, app_id)
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        with db_for(mode) as db:
            db.execute("UPDATE graph_review_drafts SET offer_details='Changed terms' WHERE id=%s", (draft,))
        assert hr.get(f'/api/graph-review-drafts/{draft}').json()['stale'] is True
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 409
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': False}).status_code == 200
        assert candidate.get(f'/api/applications/{app_id}/offers').json() == []


def test_offer_approval_race_and_failure_rollback(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        app_id = complete(mode, candidate, hr)
        first, second = proposal(hr, app_id), proposal(hr, app_id)
        for draft in (first, second):
            assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT deny_offer CHECK (action <> 'offer_presented')")
        assert hr.post(f'/api/graph-review-drafts/{first}/resume', json={'reviewed': True}).status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM job_offers').fetchone()['n'] == 0
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (first,)).fetchone()['status'] == 'pending'
            db.execute('ALTER TABLE audit_events DROP CONSTRAINT deny_offer')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(hr.post, f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}) for draft in (first, second)]
            assert sorted(item.result(timeout=30).status_code for item in results) == [200, 409]
        assert len(candidate.get(f'/api/applications/{app_id}/offers').json()) == 1


def test_prior_constraint_is_migrated_on_restart(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'hr')
    with db_for(mode) as db:
        db.execute('ALTER TABLE graph_review_drafts DROP CONSTRAINT graph_review_drafts_intent_check')
        db.execute("ALTER TABLE graph_review_drafts ADD CONSTRAINT graph_review_drafts_intent_check CHECK (intent IN ('review','advance','reserve_slot'))")
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = complete(mode, candidate, hr)
        assert proposal(hr, app_id) > 0
