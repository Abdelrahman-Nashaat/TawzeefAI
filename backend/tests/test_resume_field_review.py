from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import recruitment
from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'review.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','test.pdf',?)""",
                (candidate, 'Name: Private Candidate\nExperience\n👩‍💻 مطوّرة بيانات في Example\nEducation\nBSc Computing')).lastrowid
        yield path, client, app_id


def payload(view, value, *, section='experience', field='role', record_ref='r-1'):
    start = view['source'].index(value)
    return {'source_sha256': view['source_sha256'], 'expected_source_context': view['source_context'], 'expected_version': view['version'],
            'fields': [{'record_ref': record_ref, 'section': section, 'field': field,
                        'start': start, 'end': start + len(value)}]}


def test_review_spans_are_codepoints_and_stale_after_source_change(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/field-review'
    initial = client.get(endpoint)
    assert initial.status_code == 200
    view = initial.json()
    assert view['status'] == 'not_reviewed' and view['version'] == 0
    assert view['offset_unit'] == 'unicode_code_points'
    assert 'Private Candidate' not in view['source']
    item = payload(view, 'مطوّرة بيانات')
    education = payload(view, 'BSc Computing', section='education', field='qualification', record_ref='r-2')
    item['fields'].extend(education['fields'])
    saved = client.put(endpoint, json=item)
    assert saved.status_code == 200
    assert saved.json()['status'] == 'current' and saved.json()['version'] == 1
    assert [field['value'] for field in saved.json()['fields']] == ['مطوّرة بيانات', 'BSc Computing']
    assert client.put(endpoint, json=item).status_code == 409
    with connect(path) as db:
        audit = db.execute("SELECT details FROM audit_events WHERE action='resume_fields_reviewed'").fetchone()[0]
        stored = db.execute('SELECT fields_json FROM resume_field_reviews').fetchone()[0]
        assert 'مطوّرة' not in audit and 'مطوّرة' not in stored
        db.execute("UPDATE applications SET resume_text='Experience\\nRole: Changed' WHERE id=?", (app_id,))
    stale = client.get(endpoint).json()
    assert stale['status'] == 'stale' and stale['version'] == 1 and stale['fields'] == []
    assert client.put(endpoint, json=item).status_code == 409
    replacement = payload(stale, 'Changed')
    assert client.put(endpoint, json=replacement).json()['version'] == 2


def test_selection_validation_and_role_separation(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/field-review'
    view = client.get(endpoint).json()
    invalid = payload(view, 'مطوّرة بيانات')
    invalid['fields'][0]['value'] = 'Injected untrusted text'
    assert client.put(endpoint, json=invalid).status_code == 422
    invalid = payload(view, '[identity field removed]')
    assert client.put(endpoint, json=invalid).status_code == 422
    invalid = payload(view, 'مطوّرة بيانات')
    invalid['fields'].append(dict(invalid['fields'][0]))
    assert client.put(endpoint, json=invalid).status_code == 422
    invalid['fields'][1].update(section='projects', field='name')
    assert client.put(endpoint, json=invalid).status_code == 422
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM resume_field_reviews').fetchone()[0] == 0
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(endpoint).status_code == 403
    assert client.put(endpoint, json=payload(view, 'مطوّرة بيانات')).status_code == 403


def test_two_writers_with_same_version_cannot_both_commit(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/field-review'
    item = payload(client.get(endpoint).json(), 'مطوّرة بيانات')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.put(endpoint, json=item).status_code, range(2)))
    assert sorted(results) == [200, 409]
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM resume_field_reviews').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='resume_fields_reviewed'").fetchone()[0] == 1


def test_audit_failure_or_session_expiry_rolls_back(setup, monkeypatch):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/field-review'
    item = payload(client.get(endpoint).json(), 'مطوّرة بيانات')
    with connect(path) as db:
        db.execute("""CREATE TRIGGER abort_review BEFORE INSERT ON audit_events
            WHEN NEW.action='resume_fields_reviewed' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    assert client.put(endpoint, json=item).status_code == 500
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM resume_field_reviews').fetchone()[0] == 0
        db.execute('DROP TRIGGER abort_review')
    calls = iter([0, 99999999999])
    monkeypatch.setattr(recruitment, 'time', SimpleNamespace(time=lambda: next(calls)))
    assert client.put(endpoint, json=item).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM resume_field_reviews').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='resume_fields_reviewed'").fetchone()[0] == 0


def test_oversized_redacted_source_is_rejected_without_error_leak(setup):
    path, client, app_id = setup
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Name: x\n' * 24000, app_id))
    response = client.get(f'/api/applications/{app_id}/resume/field-review')
    assert response.status_code == 422
    assert 'Private Candidate' not in response.text
