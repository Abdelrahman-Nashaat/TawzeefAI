"""HR can explicitly advance using a current, audited rubric review."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def review(hr, app_id, score=70):
    source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
    start = next(i for i, c in enumerate(source['resume_text']) if not c.isspace())
    names = ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']
    items = [{'category': n, 'score': None} for n in names]
    if score is not None:
        items[0] = {'category': names[0], 'score': score, 'start': start, 'end': start + 1}
    response = hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items})
    assert response.status_code == 200, response.text
    event_id = response.json()['audit_event_id']
    assert hr.get(f'/api/applications/{app_id}/rubric-history').json()['items'][0]['audit_event_id'] == event_id
    return event_id


def application(candidate):
    job = candidate.get('/api/jobs').json()[0]['id']
    result = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
    assert result.status_code == 201
    return result.json()['id'], job


def test_explicit_reviewed_rubric_advance_and_audit(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate'); login(hr, 'hr')
        app_id, _ = application(candidate)
        event = review(hr, app_id)
        url = f'/api/applications/{app_id}/decision'
        assert candidate.post(url, json={'decision': 'advance', 'rubric_event_id': event}).status_code == 403
        response = hr.post(url, json={'decision': 'advance', 'rubric_event_id': event})
        assert response.status_code == 200, response.text
        assert candidate.get('/api/applications').json()[0]['status'] == 'advanced'
        with db_for(mode) as db:
            audit = db.execute("SELECT details FROM audit_events WHERE application_id=%s AND action='decision'", (app_id,)).fetchone()
            assert f'"rubric_event_id": {event}' in audit['details']


def test_old_stale_unknown_and_tampered_reviews_denied(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate'); login(hr, 'hr')
        app_id, job = application(candidate)
        url = f'/api/applications/{app_id}/decision'
        unknown = review(hr, app_id, None)
        assert hr.post(url, json={'decision': 'advance', 'rubric_event_id': unknown}).status_code == 409
        old = review(hr, app_id)
        latest = review(hr, app_id, 80)
        assert hr.post(url, json={'decision': 'advance', 'rubric_event_id': old}).status_code == 409
        with db_for(mode) as db:
            db.execute("UPDATE jobs SET title=title || ' changed' WHERE id=%s", (job,))
        assert hr.post(url, json={'decision': 'advance', 'rubric_event_id': latest}).status_code == 409
        latest = review(hr, app_id)
        with db_for(mode) as db:
            db.execute("UPDATE hr_rubric_history SET review_json='{}' WHERE audit_event_id=%s", (latest,))
        assert hr.post(url, json={'decision': 'advance', 'rubric_event_id': latest}).status_code == 409
        assert candidate.get('/api/applications').json()[0]['status'] == 'submitted'


def test_expired_session_during_scheduling_lock_rolls_back(mode, monkeypatch):  # noqa: F811
    import time
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from test_postgres_scheduling import wait_for_lock

    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate'); login(hr, 'hr')
        app_id, _ = application(candidate)
        event = review(hr, app_id)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/applications/{app_id}/decision', json={'decision': 'advance', 'rubric_event_id': event})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action='decision'", (app_id,)).fetchone()['n'] == 0
