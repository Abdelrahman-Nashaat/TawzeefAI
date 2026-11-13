"""Life-cycle recommendations use the current, reviewed PostgreSQL snapshot."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, mode  # noqa: F401
from test_postgres_scheduling import ready_booking

from app.main import create_app


def prepare_review(hr, app_id):
    result = hr.post(f'/api/applications/{app_id}/graph-review-preparations', json={})
    assert result.status_code == 201, result.text
    draft = result.json()['id']
    prepared = hr.post(f'/api/graph-review-drafts/{draft}/prepare-all')
    assert prepared.status_code == 200, prepared.text
    return draft, prepared.json()['preparation']['suggested_step']


def test_recommendation_follows_booking_and_confirmed_interview(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, slot = ready_booking(candidate, hr)
        initial_draft, step = prepare_review(hr, app_id)
        assert step == 'candidate_can_book'
        booked = candidate.post(f'/api/slots/{slot}/book', json={'application_id': app_id})
        assert booked.status_code == 201, booked.text
        assert hr.get(f'/api/graph-review-drafts/{initial_draft}').json()['stale'] is True
        assert hr.post(f'/api/graph-review-drafts/{initial_draft}/resume', json={'reviewed': True}).status_code == 409
        _, step = prepare_review(hr, app_id)
        assert step == 'review_scheduled_interview'
        # Move only the synthetic appointment into the past, so both the API
        # clock and PostgreSQL's clock agree that attendance may be confirmed.
        with db_for(mode) as db:
            db.execute('UPDATE interviews SET start_at=%s,end_at=%s WHERE id=%s',
                       (datetime.now(UTC) - timedelta(days=2), datetime.now(UTC) - timedelta(days=1), booked.json()['id']))
        finished = hr.post(f"/api/interviews/{booked.json()['id']}/complete")
        assert finished.status_code == 200, finished.text
        _, step = prepare_review(hr, app_id)
        assert step == 'review_offer_or_next_interview'
        offer = hr.post(f'/api/applications/{app_id}/offers', json={'details': 'Synthetic offer'})
        assert offer.status_code == 201, offer.text
        _, step = prepare_review(hr, app_id)
        assert step == 'review_offer_status'


def test_older_v3_preparation_remains_resumable(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, _ = ready_booking(candidate, hr)
        response = hr.post(f'/api/applications/{app_id}/graph-review-preparations', json={})
        draft = response.json()['id']
        with db_for(mode) as db:
            db.execute('UPDATE graph_review_drafts SET checkpoint_version=3 WHERE id=%s', (draft,))
        matching = hr.post(f'/api/graph-review-drafts/{draft}/prepare')
        assert matching.status_code == 200, matching.text
        assert hr.get(f'/api/graph-review-drafts/{draft}').json()['preparation']['next'] == 'status'
        prepared = hr.post(f'/api/graph-review-drafts/{draft}/prepare-all')
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()['preparation']['suggested_step'] == 'candidate_can_book'
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 200
