"""References bind HR decisions to exactly the reviewed masked source and state."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "reviews.sqlite3"
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','synthetic.pdf','Julia Smith\nPython SQL')", (candidate,)).lastrowid
        yield client, path, application


def preview(client, app_id):
    response = client.post(f'/api/applications/{app_id}/hr-review-preview')
    assert response.status_code == 200, response.text
    return response.json()


def decide(client, app_id, reference, **kwargs):
    return client.post(f'/api/applications/{app_id}/decision', json={
        'decision': kwargs.get('decision', 'advance'), 'note': kwargs.get('note', ''),
        'review_reference': reference,
    })


def test_review_binds_masked_source_reject_reason_and_single_use(context):
    client, path, app_id = context
    mask = client.get(f'/api/applications/{app_id}/resume/mask').json()
    payload = {'source_sha256': mask['source_sha256'], 'expected_source_revision': mask['source_revision'],
               'expected_revision': mask['revision'], 'spans': [{'start': 0, 'end': 11}]}
    assert client.put(f'/api/applications/{app_id}/resume/mask', json=payload).status_code == 200
    shown = preview(client, app_id)
    assert shown['source_status'] == 'reviewed' and 'Julia Smith' not in shown['source']
    assert decide(client, app_id, shown['reference'], decision='reject').status_code == 422
    result = decide(client, app_id, shown['reference'], decision='reject', note='Insufficient relevant evidence')
    assert result.status_code == 200 and result.json()['status'] == 'rejected'
    assert decide(client, app_id, shown['reference'], decision='reject', note='retry').status_code == 409
    with connect(path) as db:
        events = db.execute("SELECT details FROM audit_events WHERE action='decision' AND application_id=?", (app_id,)).fetchall()
        assert len(events) == 1 and 'Insufficient relevant evidence' in events[0]['details']
        assert db.execute('SELECT count(*) FROM hr_review_references').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['source', 'job', 'status', 'mask', 'evidence', 'criterion'])
def test_change_and_revert_or_review_change_rejects_stale_reference(context, change):
    client, path, app_id = context
    ref = preview(client, app_id)['reference']
    if change == 'mask':
        view = client.get(f'/api/applications/{app_id}/resume/mask').json()
        assert client.put(f'/api/applications/{app_id}/resume/mask', json={
            'source_sha256': view['source_sha256'], 'expected_source_revision': view['source_revision'],
            'expected_revision': view['revision'], 'spans': []}).status_code == 200
    elif change == 'evidence':
        assert client.post(f'/api/applications/{app_id}/rubric/evidence', json={'start': 0, 'end': 5}).status_code == 201
    elif change == 'criterion':
        assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'mandatory', 'label': 'Python'}).status_code == 201
    else:
        with connect(path) as db:
            if change == 'source':
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Changed', app_id))
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Julia Smith\nPython SQL', app_id))
            elif change == 'job':
                db.execute("UPDATE jobs SET title='Changed' WHERE id='job-backend-python'")
                db.execute("UPDATE jobs SET title='Python Backend Developer' WHERE id='job-backend-python'")
            else:
                db.execute("UPDATE applications SET status='advanced' WHERE id=?", (app_id,))
                db.execute("UPDATE applications SET status='submitted' WHERE id=?", (app_id,))
    assert decide(client, app_id, ref).status_code == 409
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='decision'").fetchone()[0] == 0


def test_expiry_session_binding_and_legacy_decision(context):
    client, path, app_id = context
    first = preview(client, app_id)['reference']
    assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
    assert decide(client, app_id, first).status_code == 409
    second = preview(client, app_id)['reference']
    with connect(path) as db:
        db.execute('UPDATE hr_review_references SET expires_at=0 WHERE reference=?', (second,))
    assert decide(client, app_id, second).status_code == 409
    third = preview(client, app_id)['reference']
    assert client.post('/api/auth/logout').status_code == 204
    assert decide(client, app_id, third).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
    assert client.post(f'/api/applications/{app_id}/decision', json={'decision': 'advance'}).status_code == 200


def test_preview_requires_hr_and_bounds_source(context):
    client, path, app_id = context
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('x' * 4001, app_id))
    assert client.post(f'/api/applications/{app_id}/hr-review-preview').status_code == 422
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.post(f'/api/applications/{app_id}/hr-review-preview').status_code == 403


def test_reference_replacement_and_expired_reference_cleanup(context):
    client, path, app_id = context
    first = preview(client, app_id)['reference']
    second = preview(client, app_id)['reference']
    assert first != second and decide(client, app_id, first).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM hr_review_references').fetchone()[0] == 1
        db.execute('UPDATE hr_review_references SET expires_at=0 WHERE reference=?', (second,))
    third = preview(client, app_id)['reference']
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM hr_review_references').fetchone()[0] == 1
    assert decide(client, app_id, third).status_code == 200


def test_concurrent_use_records_only_one_decision(context):
    client, path, app_id = context
    ref = preview(client, app_id)['reference']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(client, app_id, ref).status_code, range(2)))
    assert sorted(results) == [200, 409]
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (app_id,)).fetchone()[0] == 1
