"""Exercise the PostgreSQL graph through the public API and real storage."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def test_graph_review_is_hr_only_and_does_not_mutate_recruitment(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        created = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
        assert created.status_code == 201
        app_id = created.json()['id']
        doc = hr.post('/api/knowledge/documents/text', json={'title': 'Leave policy', 'text': 'Annual leave has twelve days.',
            'audience': 'hr', 'collection': 'handbook', 'page': 2})
        assert doc.status_code == 201, doc.text
        url = f'/api/applications/{app_id}/graph-review'
        assert candidate.post(url, json={'policy_question': 'annual leave'}).status_code == 403
        response = hr.post(url, json={'policy_question': 'annual leave'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['application_id'] == app_id
        assert data['status']['application_status'] == 'submitted'
        assert data['matching']['status'] == 'unavailable'
        assert data['scheduling']['slots'] == []
        assert data['policy']['sources'][0]['title'] == 'Leave policy'
        assert data['policy']['sources'][0]['page'] == 2
        assert 'sections' in data['parsed']
        assert hr.post(f"/api/knowledge/documents/{doc.json()['id']}/deactivate").status_code == 200
        assert hr.post(url, json={'policy_question': 'annual leave'}).json()['policy']['status'] == 'abstain'
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT count(*) AS n FROM interviews').fetchone()['n'] == 0


def test_graph_includes_current_review_but_not_stale_review(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
        start = next(i for i, char in enumerate(source['resume_text']) if not char.isspace())
        items = [{'category': key, 'score': None} for key in ('mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education')]
        items[0] = {'category': 'mandatory_skills', 'score': 75, 'start': start, 'end': start + 1}
        assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).status_code == 200
        url = f'/api/applications/{app_id}/graph-review'
        matching = hr.post(url, json={}).json()['matching']
        assert matching == {'status': 'current', 'range': [30, 90]}
        with db_for(mode) as db:
            db.execute("UPDATE applications SET resume_text=resume_text || ' updated' WHERE id=%s", (app_id,))
        assert hr.post(url, json={}).json()['matching']['status'] == 'unavailable'


def test_graph_preview_does_not_offer_slots_when_already_booked(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, slot_id = ready_booking(candidate, hr)
        start = datetime.now(UTC) + timedelta(days=3)
        other = hr.post('/api/slots', json={'start_at': start.isoformat(),
            'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Another'})
        assert other.status_code == 201
        url = f'/api/applications/{app_id}/graph-review'
        assert len(hr.post(url, json={}).json()['scheduling']['slots']) == 2
        assert candidate.post(f'/api/slots/{slot_id}/book', json={'application_id': app_id}).status_code == 201
        booked = hr.post(url, json={}).json()
        assert booked['scheduling']['slots'] == []
        assert booked['status']['latest_interview'] == 'scheduled'


def test_graph_denies_session_expiring_during_lock_wait(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, _ = ready_booking(candidate, hr)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/applications/{app_id}/graph-review', json={})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
