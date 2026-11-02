import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

import app.provisional_queue as queue_module
from app.db import connect
from app.main import create_app

JOB = 'job-backend-python'
QUEUE = f'/api/jobs/{JOB}/provisional-queue'


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def submit(client, text='Python Smith built Python services and SQL databases.'):
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    response = client.post('/api/applications', params={'job_id': JOB, 'filename': 'resume.docx'},
                           content=buffer.getvalue())
    assert response.status_code == 201, response.text
    return response.json()['id']


def review_mask(client, app_id, spans):
    path = f'/api/applications/{app_id}/resume/mask'
    view = client.get(path).json()
    result = client.put(path, json={'source_sha256': view['source_sha256'],
        'expected_source_revision': view['source_revision'], 'expected_revision': view['revision'], 'spans': spans})
    assert result.status_code == 200, result.text


def test_upload_queue_mask_job_and_source_revision(tmp_path):
    app = create_app(tmp_path / 'queue.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.get(QUEUE).status_code == 401
        login(client, 'candidate')
        app_id = submit(client)
        assert client.get(QUEUE).status_code == 403
        assert client.post(f'/api/applications/{app_id}/provisional-screening', json={'expected_snapshot_id': None}).status_code == 403
        login(client, 'hr')
        item = client.get(QUEUE).json()['items'][0]
        initial_id = item['snapshot_id']
        assert item['recommendation'] == 'pending'
        assert item['source_review_status'] == 'unreviewed'
        assert not item['current'] and item['score'] is None
        assert 'Python Smith' not in str(client.get(QUEUE).json())
        assert client.put(f'/api/jobs/{JOB}/provisional-threshold', json={'threshold': 40, 'expected_version': 0}).status_code == 200
        review_mask(client, app_id, [{'start': 0, 'end': len('Python Smith')}])
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'pending'
        refresh = f'/api/applications/{app_id}/provisional-screening'
        response = client.post(refresh, json={'expected_snapshot_id': initial_id})
        assert response.status_code == 201
        second_id = response.json()['snapshot_id']
        assert client.get(QUEUE).json()['items'][0]['recommendation'] in ('priority_review', 'needs_review')
        assert client.post(refresh, json={'expected_snapshot_id': initial_id}).status_code == 409
        review_mask(client, app_id, [])
        review_mask(client, app_id, [{'start': 0, 'end': len('Python Smith')}])
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'stale'
        assert client.post(refresh, json={'expected_snapshot_id': second_id}).status_code == 201
        with connect(app.state.database_path) as db:
            title = db.execute('SELECT title FROM jobs WHERE id=?', (JOB,)).fetchone()[0]
            db.execute('UPDATE jobs SET title=? WHERE id=?', ('temporary', JOB))
            db.execute('UPDATE jobs SET title=? WHERE id=?', (title, JOB))
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'stale'
        newest_id = client.get(QUEUE).json()['items'][0]['snapshot_id']
        assert client.post(refresh, json={'expected_snapshot_id': newest_id}).status_code == 201
        with connect(app.state.database_path) as db:
            db.execute('UPDATE applications SET job_id=? WHERE id=?', ('job-data-engineer', app_id))
            db.execute('UPDATE applications SET job_id=? WHERE id=?', (JOB, app_id))
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'stale'
        history = client.get(f'/api/applications/{app_id}/provisional-history').json()['items']
        assert len(history) == 4 and history[-1]['id'] == initial_id
        assert history[-1]['source_review_status'] == 'unreviewed'
        assert 'Python Smith' not in str(history)
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT status,score_json FROM applications WHERE id=?', (app_id,)).fetchone()[:] == ('submitted', None)
            assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (app_id,)).fetchone()[0] == 0
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Changed', app_id))
        assert client.get(QUEUE).json()['items'][0]['source_review_status'] == 'stale'


def test_refresh_conflicts_and_finalized_applications(tmp_path):
    app = create_app(tmp_path / 'race.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        app_id = submit(client)
        login(client, 'hr')
        latest = client.get(QUEUE).json()['items'][0]['snapshot_id']
        refresh = f'/api/applications/{app_id}/provisional-screening'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: client.post(refresh, json={'expected_snapshot_id': latest}).status_code, range(2)))
        assert sorted(results) == [201, 409]
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT count(*) FROM provisional_snapshots WHERE application_id=?', (app_id,)).fetchone()[0] == 2
            db.execute("UPDATE applications SET status='rejected' WHERE id=?", (app_id,))
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'finalized'
        latest = client.get(QUEUE).json()['items'][0]['snapshot_id']
        assert client.post(refresh, json={'expected_snapshot_id': latest}).status_code == 409


def test_upload_rolls_back_if_snapshot_storage_fails(tmp_path):
    app = create_app(tmp_path / 'rollback.sqlite3', seed_demo=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client, 'candidate')
        with connect(app.state.database_path) as db:
            db.execute('''CREATE TRIGGER fail_provisional BEFORE INSERT ON provisional_snapshots
                          BEGIN SELECT RAISE(ABORT, 'test failure'); END''')
        document = Document()
        document.add_paragraph('Python SQL')
        buffer = BytesIO()
        document.save(buffer)
        response = client.post('/api/applications', params={'job_id': JOB, 'filename': 'resume.docx'},
                               content=buffer.getvalue())
        assert response.status_code == 500
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM audit_events').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM provisional_snapshots').fetchone()[0] == 0


def test_upload_and_listing_never_redact_or_score_and_large_inputs_stay_pending(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'bounds.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        def forbidden(*args, **kwargs):
            raise AssertionError('upload and queue must not redact or score')
        with monkeypatch.context() as patch:
            patch.setattr(queue_module, 'masked_source', forbidden)
            patch.setattr(queue_module, 'score_masked_resume', forbidden)
            large = submit(client, 'x' * 200_000)
            short = submit(client, 'Python SQL')
            login(client, 'hr')
            items = client.get(QUEUE).json()['items']
            assert [item['recommendation'] for item in items] == ['pending', 'pending']
            result = client.post(f'/api/applications/{large}/provisional-screening',
                                 json={'expected_snapshot_id': items[0]['snapshot_id']})
            assert result.status_code == 422 and 'manually' in result.json()['detail']
            with connect(app.state.database_path) as db:
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Python\0' + 'x' * 200_000, large))
            result = client.post(f'/api/applications/{large}/provisional-screening',
                                 json={'expected_snapshot_id': items[0]['snapshot_id']})
            assert result.status_code == 422 and 'manually' in result.json()['detail']
            with connect(app.state.database_path) as db:
                db.execute('UPDATE jobs SET required_skills=? WHERE id=?',
                           (json.dumps([f'skill {n}' for n in range(100)]), JOB))
            result = client.post(f'/api/applications/{short}/provisional-screening',
                                 json={'expected_snapshot_id': items[1]['snapshot_id']})
            assert result.status_code == 422 and 'manually' in result.json()['detail']
            assert all(item['recommendation'] == 'pending' for item in client.get(QUEUE).json()['items'])
            with connect(app.state.database_path) as db:
                assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 2
                assert db.execute("SELECT count(*) FROM provisional_snapshots WHERE snapshot_kind='pending'").fetchone()[0] == 2


def test_scoring_rechecks_source_change_and_revert_without_holding_write_lock(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'source-race.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        app_id = submit(client, 'Python SQL')
        login(client, 'hr')
        item = client.get(QUEUE).json()['items'][0]
        original = queue_module.score_masked_resume
        def change_and_revert(job, source):
            with connect(app.state.database_path) as db:
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('changed', app_id))
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Python SQL', app_id))
            return original(job, source)
        monkeypatch.setattr(queue_module, 'score_masked_resume', change_and_revert)
        result = client.post(f'/api/applications/{app_id}/provisional-screening',
                             json={'expected_snapshot_id': item['snapshot_id']})
        assert result.status_code == 409
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'pending'
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT count(*) FROM provisional_snapshots WHERE application_id=?', (app_id,)).fetchone()[0] == 1


def test_scoring_rechecks_authorization_after_computation(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'role-race.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        app_id = submit(client, 'Python SQL')
        login(client, 'hr')
        item = client.get(QUEUE).json()['items'][0]
        original = queue_module.score_masked_resume
        def revoke_during_scoring(job, source):
            with connect(app.state.database_path) as db:
                db.execute("UPDATE users SET role='candidate' WHERE username='hr'")
            return original(job, source)
        monkeypatch.setattr(queue_module, 'score_masked_resume', revoke_during_scoring)
        result = client.post(f'/api/applications/{app_id}/provisional-screening',
                             json={'expected_snapshot_id': item['snapshot_id']})
        assert result.status_code == 403
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT count(*) FROM provisional_snapshots WHERE application_id=?', (app_id,)).fetchone()[0] == 1


def test_no_redaction_and_keyset_pagination(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'pages.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        first = submit(client, 'Python SQL')
        login(client, 'hr')
        pending = client.get(QUEUE).json()['items'][0]
        assert client.post(f'/api/applications/{first}/provisional-screening',
                           json={'expected_snapshot_id': pending['snapshot_id']}).status_code == 201
        item = client.get(QUEUE).json()['items'][0]
        assert item['automatic_masked'] is False
        assert item['source_review_status'] == 'unreviewed'
        assert item['recommendation'] == 'mask_review_required'
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            for _ in range(54):
                db.execute('INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,?,?,?)',
                           (candidate, JOB, 'synthetic.docx', 'Python'))
        monkeypatch.setattr(queue_module, 'masked_source', lambda *args: (_ for _ in ()).throw(AssertionError('unexpected scan')))
        seen, cursor = [], None
        while True:
            response = client.get(QUEUE, params={'limit': 20, **({'after': cursor} if cursor else {})})
            assert response.status_code == 200
            page = response.json()
            assert len(page['items']) <= 20
            seen.extend(item['application_id'] for item in page['items'])
            cursor = page['next_after']
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 55 and seen == sorted(seen)
        assert client.get(QUEUE, params={'limit': 51}).status_code == 422


def test_exact_work_limits_include_the_complete_resume_and_all_skills(tmp_path):
    app = create_app(tmp_path / 'limit-boundary.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        app_id = submit(client, ('x' * 3993) + ' Python')
        login(client, 'hr')
        with connect(app.state.database_path) as db:
            db.execute('UPDATE jobs SET required_skills=?, preferred_skills=? WHERE id=?',
                       (json.dumps(['Python'] + [f'skill {i}' for i in range(19)]), '[]', JOB))
        item = client.get(QUEUE).json()['items'][0]
        result = client.post(f'/api/applications/{app_id}/provisional-screening',
                             json={'expected_snapshot_id': item['snapshot_id']})
        assert result.status_code == 201, result.text
        item = client.get(QUEUE).json()['items'][0]
        assert item['score'] == 5
        with connect(app.state.database_path) as db:
            result_row = db.execute('SELECT required_mentioned,required_total FROM provisional_snapshots WHERE id=?',
                                    (item['snapshot_id'],)).fetchone()
            assert tuple(result_row) == (1, 20)
            db.execute('UPDATE jobs SET required_skills=? WHERE id=?', (json.dumps(['x' * 81]), JOB))
        result = client.post(f'/api/applications/{app_id}/provisional-screening',
                             json={'expected_snapshot_id': item['snapshot_id']})
        assert result.status_code == 422
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'stale'
