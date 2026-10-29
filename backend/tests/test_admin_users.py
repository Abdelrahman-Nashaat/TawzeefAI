from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from fastapi.testclient import TestClient

from app.admin_users import bootstrap_admin, initialize_admin_users
from app.auth import hash_password
from app.db import connect, initialize
from app.main import create_app

ADMIN_PASSWORD = 'local-admin-password'


def login(client, username, password):
    return client.post('/api/auth/login', json={'username': username, 'password': password})


def test_nondestructive_migration_and_bootstrap_restart(tmp_path):
    path = tmp_path / 'old.sqlite3'
    initialize(path)
    with connect(path) as db:
        uid = db.execute('INSERT INTO users(username,display_name,role,password_hash) VALUES (?,?,?,?)',
                         ('old', 'Existing', 'candidate', hash_password('existing-password'))).lastrowid
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', ('oldjob', 'Old', 'Cairo', '[]', '[]', '[]'))
        app_id = db.execute('INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,?,?,?)',
                            (uid, 'oldjob', 'cv.pdf', 'old text')).lastrowid
        db.execute("INSERT INTO sessions VALUES ('historic-hash', ?, 9999999999)", (uid,))
    initialize_admin_users(path)
    initialize_admin_users(path)
    admin_id = bootstrap_admin(path, 'localadmin', 'Local Admin', ADMIN_PASSWORD)
    with connect(path) as db:
        assert db.execute('SELECT candidate_id FROM applications WHERE id=?', (app_id,)).fetchone()[0] == uid
        assert db.execute('SELECT active FROM users WHERE id=?', (uid,)).fetchone()[0] == 1
        assert db.execute("SELECT user_id FROM sessions WHERE token_hash='historic-hash'").fetchone()[0] == uid
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    try:
        bootstrap_admin(path, 'second', 'Second', ADMIN_PASSWORD)
        assert False, 'bootstrap must not replace an existing admin'
    except ValueError:
        pass
    for _ in range(2):
        with TestClient(create_app(path, seed_demo=False)) as client:
            assert login(client, 'localadmin', ADMIN_PASSWORD).status_code == 200
            assert client.get('/api/admin/access').json() == {'is_admin': True}
            assert client.get('/api/admin/users').status_code == 200
            assert client.get('/api/audit').status_code == 403
            assert client.get('/api/admin/users').json()[-1]['id'] == admin_id


def test_admin_access_updates_revocation_and_password_reset(tmp_path):
    path = tmp_path / 'admin.sqlite3'
    bootstrap_admin(path, 'localadmin', 'Local Admin', ADMIN_PASSWORD)
    with TestClient(create_app(path, seed_demo=True)) as admin, TestClient(create_app(path, seed_demo=True)) as target:
        assert admin.get('/api/admin/users').status_code == 401
        assert login(admin, 'hr', 'hr-demo-only').status_code == 200
        assert admin.get('/api/admin/access').json() == {'is_admin': False}
        assert admin.get('/api/admin/users').status_code == 403
        assert admin.get('/api/admin/audit').status_code == 403
        assert login(admin, 'localadmin', ADMIN_PASSWORD).status_code == 200
        assert admin.get('/api/audit').status_code == 403
        created = admin.post('/api/admin/users', json={'username': 'managed', 'display_name': 'Managed User',
            'password': 'managed-password', 'role': 'candidate'})
        assert created.status_code == 201 and 'password' not in str(created.json())
        user_id = created.json()['id']
        assert login(target, 'managed', 'managed-password').status_code == 200
        token = target.cookies.get('tawzeef_session')
        assert admin.patch(f'/api/admin/users/{user_id}', json={'active': False}).status_code == 200
        assert target.get('/api/auth/me').status_code == 401
        assert login(target, 'managed', 'managed-password').status_code == 401
        assert admin.patch(f'/api/admin/users/{user_id}', json={'active': True}).status_code == 200
        target.cookies.set('tawzeef_session', token)
        assert target.get('/api/auth/me').status_code == 401
        assert login(target, 'managed', 'managed-password').status_code == 200
        assert admin.patch(f'/api/admin/users/{user_id}', json={'role': 'employee'}).status_code == 200
        assert target.get('/api/auth/me').status_code == 401
        assert login(target, 'managed', 'managed-password').json()['role'] == 'employee'
        assert admin.post(f'/api/admin/users/{user_id}/password', json={'password': 'replacement-password'}).status_code == 200
        assert target.get('/api/auth/me').status_code == 401
        assert login(target, 'managed', 'managed-password').status_code == 401
        assert login(target, 'managed', 'replacement-password').status_code == 200
        admin_id = admin.get('/api/auth/me').json()['id']
        assert admin.patch(f'/api/admin/users/{admin_id}', json={'active': False}).status_code == 409
        assert admin.patch(f'/api/admin/users/{admin_id}', json={'is_admin': False}).status_code == 409
        with connect(path) as db:
            audit = [dict(row) for row in db.execute('SELECT * FROM user_admin_audit')]
            assert 'replacement-password' not in str(audit)
            assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_concurrent_admin_removal_cannot_remove_last_active_admin(tmp_path):
    path = tmp_path / 'concurrency.sqlite3'
    admin_one = bootstrap_admin(path, 'firstadmin', 'First Admin', ADMIN_PASSWORD)
    with TestClient(create_app(path, seed_demo=False)) as first, TestClient(create_app(path, seed_demo=False)) as second:
        assert login(first, 'firstadmin', ADMIN_PASSWORD).status_code == 200
        created = first.post('/api/admin/users', json={'username': 'secondadmin', 'display_name': 'Second Admin',
            'password': 'second-admin-password', 'role': 'employee', 'is_admin': True})
        assert created.status_code == 201
        admin_two = created.json()['id']
        assert login(second, 'secondadmin', 'second-admin-password').status_code == 200
        barrier = Barrier(2)

        def remove(client, user_id):
            barrier.wait(timeout=10)
            return client.patch(f'/api/admin/users/{user_id}', json={'is_admin': False}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(remove, first, admin_two)
            two = pool.submit(remove, second, admin_one)
            results = [one.result(), two.result()]
            assert results.count(200) == 1 and next(code for code in results if code != 200) in (401, 403)
        with connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM user_admins a JOIN users u ON u.id=a.user_id WHERE u.active=1').fetchone()[0] == 1


def test_login_rechecks_disabled_account_and_changed_password_after_verification(tmp_path, monkeypatch):
    import app.main as main_module

    path = tmp_path / 'login-race.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        original = main_module.verify_password
        for change in ('disable', 'password'):
            with connect(path) as db:
                db.execute('UPDATE users SET active=1, password_hash=? WHERE username=?',
                           (hash_password('candidate-demo-only'), 'candidate'))

            def verify_then_change(password, encoded, change=change):
                valid = original(password, encoded)
                with connect(path) as db:
                    if change == 'disable':
                        db.execute("UPDATE users SET active=0 WHERE username='candidate'")
                    else:
                        db.execute('UPDATE users SET password_hash=? WHERE username=?',
                                   (hash_password('new-candidate-password'), 'candidate'))
                return valid

            monkeypatch.setattr(main_module, 'verify_password', verify_then_change)
            assert login(client, 'candidate', 'candidate-demo-only').status_code == 401
            assert client.get('/api/auth/me').status_code == 401


def test_admin_is_rechecked_after_slow_hashing_before_write(tmp_path, monkeypatch):
    import app.admin_users as admin_module

    path = tmp_path / 'admin-revoke-race.sqlite3'
    first_id = bootstrap_admin(path, 'firstadmin', 'First Admin', ADMIN_PASSWORD)
    with TestClient(create_app(path, seed_demo=False)) as first, TestClient(create_app(path, seed_demo=False)) as second:
        assert login(first, 'firstadmin', ADMIN_PASSWORD).status_code == 200
        assert first.post('/api/admin/users', json={'username': 'secondadmin', 'display_name': 'Second Admin',
            'password': 'second-admin-password', 'role': 'employee', 'is_admin': True}).status_code == 201
        assert login(second, 'secondadmin', 'second-admin-password').status_code == 200
        started, release = Event(), Event()
        original = admin_module.hash_password

        def blocked_hash(password):
            started.set()
            assert release.wait(timeout=30)
            return original(password)

        monkeypatch.setattr(admin_module, 'hash_password', blocked_hash)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first.post, '/api/admin/users', json={'username': 'should_not_exist',
                'display_name': 'Should Not Exist', 'password': 'some-secure-password', 'role': 'employee'})
            assert started.wait(timeout=30)
            assert second.patch(f'/api/admin/users/{first_id}', json={'is_admin': False}).status_code == 200
            release.set()
            assert pending.result().status_code == 403
        with connect(path) as db:
            assert db.execute("SELECT 1 FROM users WHERE username='should_not_exist'").fetchone() is None
