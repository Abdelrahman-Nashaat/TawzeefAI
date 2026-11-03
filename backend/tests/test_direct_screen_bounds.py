"""Direct HR screening releases the DB lock and rejects stale work."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app import recruitment
from app.db import connect
from app.main import create_app


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def application(db, text='Experience: Built Python services'):
    owner = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
    return db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','fictional.pdf',?)", (owner, text)).lastrowid


@pytest.mark.parametrize('change,expected', [('source_revert',409), ('job_revert',409), ('mask',409), ('owner_revert',409), ('status_revert',409), ('revoke',401), ('role',403)])
def test_screen_rechecks_changes_without_holding_writer_lock(tmp_path, monkeypatch, change, expected):
    path=tmp_path/'race.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client, TestClient(create_app(path, seed_demo=True)) as other:
        login(client,'hr'); login(other,'hr')
        with connect(path) as db:
            assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
            appid=application(db)
            original_job=db.execute("SELECT required_skills FROM jobs WHERE id='job-backend-python'").fetchone()[0]
        started=Event(); release=Event(); original=recruitment.score_masked_resume
        def delayed(job,source):
            started.set()
            assert release.wait(30), 'test did not release scoring'
            return original(job,source)
        monkeypatch.setattr(recruitment,'score_masked_resume',delayed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,f'/api/applications/{appid}/screen')
            assert started.wait(30)
            try:
                if change == 'mask':
                    endpoint=f'/api/applications/{appid}/resume/mask'
                    mask=other.get(endpoint).json()
                    response=other.put(endpoint,json={'source_sha256':mask['source_sha256'], 'expected_source_revision':mask['source_revision'], 'expected_revision':mask['revision'], 'spans':[]})
                    assert response.status_code == 200
                else:
                    # This independent writer must commit while scoring is paused.
                    with connect(path) as db:
                        if change == 'source_revert':
                            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Changed',appid))
                            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Experience: Built Python services',appid))
                        elif change == 'job_revert':
                            db.execute("UPDATE jobs SET required_skills='[]' WHERE id='job-backend-python'")
                            db.execute("UPDATE jobs SET required_skills=? WHERE id='job-backend-python'",(original_job,))
                        elif change == 'owner_revert':
                            employee=db.execute("SELECT id FROM users WHERE username='employee'").fetchone()[0]
                            owner=db.execute('SELECT candidate_id FROM applications WHERE id=?',(appid,)).fetchone()[0]
                            db.execute('UPDATE applications SET candidate_id=? WHERE id=?',(employee,appid))
                            db.execute('UPDATE applications SET candidate_id=? WHERE id=?',(owner,appid))
                        elif change == 'status_revert':
                            db.execute("UPDATE applications SET status='advanced' WHERE id=?",(appid,))
                            db.execute("UPDATE applications SET status='submitted' WHERE id=?",(appid,))
                        elif change == 'revoke':
                            db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
                        else:
                            db.execute("UPDATE users SET role='employee' WHERE username='hr'")
            finally:
                release.set()
            response=future.result(timeout=30)
        assert response.status_code == expected, response.text
        with connect(path) as db:
            assert db.execute('SELECT score_json FROM applications WHERE id=?',(appid,)).fetchone()[0] is None
            assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='screened'",(appid,)).fetchone()[0] == 0


def test_max_supported_source_and_100_skills_are_not_truncated(tmp_path):
    path=tmp_path/'max.sqlite3'
    with TestClient(create_app(path,seed_demo=True)) as client:
        login(client,'hr')
        skills=[f'keyword{i}' for i in range(100)]
        with connect(path) as db:
            db.execute("UPDATE jobs SET required_skills=?, preferred_skills=? WHERE id='job-backend-python'",(json.dumps(skills[:50]),json.dumps(skills[50:])))
            source=('x\n'*99997)+'Python'
            assert len(source)<=200000
            appid=application(db,source)
        response=client.post(f'/api/applications/{appid}/screen')
        assert response.status_code == 200, response.text
        assert len(response.json()['required']) == 50 and len(response.json()['preferred']) == 50


def test_expanded_redaction_over_work_budget_returns_no_score(tmp_path):
    path=tmp_path/'budget.sqlite3'
    with TestClient(create_app(path,seed_demo=True)) as client:
        login(client,'hr')
        skills=[f'keyword{i}' for i in range(100)]
        with connect(path) as db:
            db.execute("UPDATE jobs SET required_skills=?, preferred_skills=? WHERE id='job-backend-python'",(json.dumps(skills[:50]),json.dumps(skills[50:])))
            appid=application(db,'Name: x\n'*25000)
        response=client.post(f'/api/applications/{appid}/screen')
        assert response.status_code == 422 and 'work limit' in response.text
        with connect(path) as db:
            assert db.execute('SELECT score_json FROM applications WHERE id=?',(appid,)).fetchone()[0] is None


def test_competing_screen_cannot_overwrite_newer_history(tmp_path, monkeypatch):
    path=tmp_path/'competing.sqlite3'
    app=create_app(path,seed_demo=True)
    with TestClient(app) as first, TestClient(app) as second:
        login(first,'hr'); login(second,'hr')
        with connect(path) as db:
            appid=application(db)
        started=Event(); release=Event(); original=recruitment.score_masked_resume
        calls=0
        def delayed_once(job,source):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                assert release.wait(30)
            return original(job,source)
        monkeypatch.setattr(recruitment,'score_masked_resume',delayed_once)
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting=pool.submit(first.post,f'/api/applications/{appid}/screen')
            assert started.wait(30)
            try:
                assert second.post(f'/api/applications/{appid}/screen').status_code == 200
            finally:
                release.set()
            assert waiting.result(timeout=30).status_code == 409
        with connect(path) as db:
            assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='screened'",(appid,)).fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM screening_history').fetchone()[0] == 1
