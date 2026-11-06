import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_duplicate_resume_signal_requires_hr_and_uses_full_normalized_text(tmp_path):
    path = tmp_path / 'duplicates.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            first = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'one.docx', ?)""",
                (candidate, 'Synthetic Résumé\nBuilt Python services.')).lastrowid
            same = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'two.docx', ?)""",
                (candidate, 'synthetic résumé   built python services.')).lastrowid
            other = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'three.docx', ?)""",
                (candidate, 'Synthetic Résumé\nBuilt Python services and SQL.')).lastrowid
            blank1 = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'empty.docx', '  ')""", (candidate,)).lastrowid
            db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'empty2.docx', '  ')""", (candidate,))
        path_url = f'/api/applications/{first}/duplicates'
        assert client.get(path_url).status_code == 401
        assert client.post('/api/auth/login', json={'username':'candidate', 'password':'candidate-demo-only'}).status_code == 200
        assert client.get(path_url).status_code == 403
        assert client.post('/api/auth/login', json={'username':'hr', 'password':'hr-demo-only'}).status_code == 200
        result = client.get(path_url)
        assert result.status_code == 200
        assert result.json()['matches'] == [{'application_id': same}]
        assert result.json()['truncated'] is False
        assert 'Python' not in result.text and 'one.docx' not in result.text
        assert client.get(f'/api/applications/{other}/duplicates').json()['matches'] == []
        assert client.get(f'/api/applications/{blank1}/duplicates').json()['matches'] == []
        assert client.get('/api/applications/999999/duplicates').status_code == 404


def test_duplicate_search_reports_bounded_scan(tmp_path):
    path = tmp_path / 'bounded.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            target = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'target.docx', 'Target text')""", (candidate,)).lastrowid
            db.executemany("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'synthetic.docx', ?)""",
                ((candidate, f'synthetic {i}') for i in range(2001)))
        assert client.post('/api/auth/login', json={'username':'hr', 'password':'hr-demo-only'}).status_code == 200
        result = client.get(f'/api/applications/{target}/duplicates').json()
        assert result['matches'] == [] and result['scanned'] == 2000
        assert result['truncated'] is True


@pytest.mark.parametrize('change', ['update', 'insert', 'logout'])
def test_duplicate_scan_rechecks_session_and_data_after_concurrent_change(tmp_path, monkeypatch, change):
    from app import recruitment

    path = tmp_path / 'race.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            ids = [db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES(?, 'job-backend-python', 'file.docx', 'Same resume')""", (candidate,)).lastrowid for _ in range(2)]
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        original = recruitment.normalize
        changed = False

        def revoke_during_scan(text):
            nonlocal changed
            if not changed:
                changed = True
                with connect(path) as db:
                    db.execute("UPDATE users SET role='employee' WHERE username='hr'")
            return original(text)

        monkeypatch.setattr(recruitment, 'normalize', revoke_during_scan)
        assert client.get(f'/api/applications/{ids[0]}/duplicates').status_code == 403

        with connect(path) as db:
            db.execute("UPDATE users SET role='hr' WHERE username='hr'")
        changed = False

        def change_match_during_scan(text):
            nonlocal changed
            if not changed:
                changed = True
                with connect(path) as db:
                    if change == 'update':
                        db.execute("UPDATE applications SET resume_text='Different now' WHERE id=?", (ids[1],))
                    elif change == 'insert':
                        db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                            VALUES(?, 'job-backend-python', 'new.docx', 'Same resume')""", (candidate,))
                    else:
                        db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
            return original(text)

        monkeypatch.setattr(recruitment, 'normalize', change_match_during_scan)
        assert client.get(f'/api/applications/{ids[0]}/duplicates').status_code == (401 if change == 'logout' else 409)
