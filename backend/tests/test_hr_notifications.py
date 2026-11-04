from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.main import create_app


def login(client, role, password=None):
    assert client.post('/api/auth/login', json={'username': role, 'password': password or f'{role}-demo-only'}).status_code == 200


def resume():
    doc = Document()
    doc.add_paragraph('Synthetic Resume: Python')
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def test_submission_inbox_is_atomic_private_per_actor_and_paginated(tmp_path):
    path = tmp_path / 'notifications.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.get('/api/hr/notifications').status_code == 401
        login(client, 'candidate')
        for _ in range(3):
            assert client.post('/api/applications', params={'job_id':'job-backend-python','filename':'synthetic.docx'}, content=resume()).status_code == 201
        assert client.get('/api/hr/notifications').status_code == 403
        assert client.post('/api/hr/notifications/1/read').status_code == 403
        login(client, 'employee')
        assert client.get('/api/hr/notifications').status_code == 403
        login(client, 'hr')
        response = client.get('/api/hr/notifications', params={'limit':2})
        assert response.status_code == 200
        data = response.json()
        assert [item['id'] for item in data['items']] == [3, 2]
        assert data['next_before'] == 2 and all(not item['is_read'] for item in data['items'])
        assert 'Synthetic Resume' not in response.text and 'candidate_id' not in response.text
        assert client.post('/api/hr/notifications/3/read').status_code == 200
        assert client.post('/api/hr/notifications/3/read').status_code == 200
        assert client.get('/api/hr/notifications').json()['items'][0]['is_read']
        assert [item['id'] for item in client.get('/api/hr/notifications', params={'before':2}).json()['items']] == [1]
        assert client.get('/api/hr/notifications', params={'limit':51}).status_code == 422
        assert client.post('/api/hr/notifications/999/read').status_code == 404
        with connect(path) as db:
            second = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('hr2','Other HR','hr',?)", (hash_password('hr2-password'),)).lastrowid
            assert db.execute('SELECT count(*) FROM hr_notification_reads').fetchone()[0] == 1
            assert db.execute("SELECT status FROM applications WHERE id=3").fetchone()[0] == 'submitted'
        login(client, 'hr2', 'hr2-password')
        assert not client.get('/api/hr/notifications').json()['items'][0]['is_read']
        assert client.post('/api/hr/notifications/3/read').status_code == 200
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM hr_notification_reads WHERE actor_id=?', (second,)).fetchone()[0] == 1


def test_legacy_and_rolled_back_uploads_do_not_create_alerts(tmp_path, monkeypatch):
    import app.application_store as store

    path = tmp_path / 'notifications.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','old.docx','old')", (candidate,))
        login(client, 'candidate')
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic rollback')
        monkeypatch.setattr(store, 'create_pending_snapshot', fail)
        try:
            client.post('/api/applications', params={'job_id':'job-backend-python','filename':'synthetic.docx'}, content=resume())
        except RuntimeError:
            pass
        else:
            raise AssertionError('submission failure was not propagated')
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM hr_notifications').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 1


def test_revoked_session_during_acknowledgment_rolls_back(tmp_path, monkeypatch):
    from fastapi import HTTPException

    import app.hr_notifications as notifications

    path = tmp_path / 'notifications.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'candidate')
        assert client.post('/api/applications', params={'job_id':'job-backend-python','filename':'synthetic.docx'}, content=resume()).status_code == 201
        login(client, 'hr')
        original = notifications._require_current_role
        calls = 0
        def authorize(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise HTTPException(status_code=401, detail='Session expired')
            return original(*args)
        monkeypatch.setattr(notifications, '_require_current_role', authorize)
        assert client.post('/api/hr/notifications/1/read').status_code == 401
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM hr_notification_reads').fetchone()[0] == 0


def test_revoked_session_during_list_does_not_disclose(tmp_path, monkeypatch):
    from fastapi import HTTPException

    import app.hr_notifications as notifications

    path = tmp_path / 'notifications.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'candidate')
        assert client.post('/api/applications', params={'job_id':'job-backend-python','filename':'synthetic.docx'}, content=resume()).status_code == 201
        login(client, 'hr')
        original = notifications._require_current_role
        calls = 0
        def authorize(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise HTTPException(status_code=401, detail='Session expired')
            return original(*args)
        monkeypatch.setattr(notifications, '_require_current_role', authorize)
        response = client.get('/api/hr/notifications')
        assert response.status_code == 401
        assert 'application_id' not in response.text


def test_list_rechecks_real_session_and_role_revocation_after_snapshot(tmp_path, monkeypatch):
    import app.hr_notifications as notifications
    from app.auth import COOKIE_NAME, token_digest

    path = tmp_path / 'wal-notifications.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
        login(client, 'candidate')
        assert client.post('/api/applications', params={'job_id': 'job-backend-python', 'filename': 'synthetic.docx'}, content=resume()).status_code == 201
        original = notifications._require_current_role
        for change, expected in (('session', 401), ('role', 403), ('inactive', 401)):
            with connect(path) as db:
                db.execute("UPDATE users SET role='hr',active=1 WHERE username='hr'")
            login(client, 'hr')
            token_hash = token_digest(client.cookies.get(COOKIE_NAME))
            calls = 0
            def authorize(db, request, user, role, _change=change, _token_hash=token_hash):
                nonlocal calls
                calls += 1
                if calls == 2:
                    with connect(path) as change_db:
                        if _change == 'session':
                            change_db.execute('DELETE FROM sessions WHERE token_hash=?', (_token_hash,))
                        elif _change == 'role':
                            change_db.execute("UPDATE users SET role='employee' WHERE username='hr'")
                        else:
                            change_db.execute("UPDATE users SET active=0 WHERE username='hr'")
                return original(db, request, user, role)
            monkeypatch.setattr(notifications, '_require_current_role', authorize)
            response = client.get('/api/hr/notifications')
            assert response.status_code == expected
            assert 'application_id' not in response.text
            assert calls == 2
            monkeypatch.setattr(notifications, '_require_current_role', original)
