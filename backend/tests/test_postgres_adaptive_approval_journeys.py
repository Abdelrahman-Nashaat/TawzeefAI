"""Adaptive review decisions stay supervised through both candidate journeys."""

from fastapi.testclient import TestClient
from test_postgres_durable_graph import assess, create_application, slot_proposal
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_offers import complete

from app.main import create_app


def test_adaptive_advance_requires_review_then_candidate_books_private_slot(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as other, TestClient(app) as hr:
        application = create_application(candidate)
        login(other, 'candidate2')
        login(hr, 'hr')
        assess(hr, application)
        proposal = hr.post(f'/api/applications/{application}/adaptive-review-preparations',
                           json={'intent': 'advance', 'slot': slot_proposal()})
        assert proposal.status_code == 201, proposal.text
        draft = proposal.json()['id']
        review = f'/api/graph-review-drafts/{draft}'
        assert other.get(review).status_code == 403
        assert hr.post(review + '/resume', json={'reviewed': True}).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT count(*) AS n FROM interview_slots').fetchone()['n'] == 0
        prepared = hr.post(review + '/prepare-all')
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()['preparation']['ready'] is True
        opened = hr.get(review)
        assert opened.status_code == 200 and opened.json()['stale'] is False
        assert {'parsed', 'matching', 'status'} <= set(opened.json()['result'])
        assert hr.post(review + '/resume', json={'reviewed': True}).status_code == 200
        assert hr.post(review + '/resume', json={'reviewed': True}).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT count(*) AS n FROM interviews').fetchone()['n'] == 0
        slots = candidate.get('/api/slots', params={'application_id': application}).json()
        assert len(slots) == 1
        assert other.post(f"/api/slots/{slots[0]['id']}/book", json={'application_id': application}).status_code == 404
        booking = candidate.post(f"/api/slots/{slots[0]['id']}/book", json={'application_id': application})
        assert booking.status_code == 201, booking.text
        assert booking.json()['application_id'] == application


def test_adaptive_offer_requires_review_then_candidate_accepts_exact_terms(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as other, TestClient(app) as hr:
        application = complete(mode, candidate, hr)
        login(other, 'candidate2')
        terms = 'Synthetic annual terms, starting next month.'
        response = hr.post(f'/api/applications/{application}/adaptive-review-preparations',
                           json={'intent': 'present_offer', 'offer_details': terms})
        assert response.status_code == 201, response.text
        draft = response.json()['id']
        review = f'/api/graph-review-drafts/{draft}'
        assert hr.post(review + '/resume', json={'reviewed': True}).status_code == 409
        assert candidate.get(f'/api/applications/{application}/offers').json() == []
        prepared = hr.post(review + '/prepare-all')
        assert prepared.status_code == 200, prepared.text
        opened = hr.get(review)
        assert opened.status_code == 200 and opened.json()['offer_details'] == terms
        assert hr.post(review + '/resume', json={'reviewed': True}).status_code == 200
        assert hr.post(review + '/resume', json={'reviewed': True}).status_code == 409
        offers = candidate.get(f'/api/applications/{application}/offers').json()
        assert len(offers) == 1 and offers[0]['details'] == terms and offers[0]['status'] == 'presented'
        assert other.post(f"/api/offers/{offers[0]['id']}/respond", json={'decision': 'accepted'}).status_code == 404
        assert candidate.post(f"/api/offers/{offers[0]['id']}/respond", json={'decision': 'accepted'}).status_code == 200
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM job_offers WHERE id=%s', (offers[0]['id'],)).fetchone()['status'] == 'accepted'
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'offered'
