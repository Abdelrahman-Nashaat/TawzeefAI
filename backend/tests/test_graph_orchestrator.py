import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from queue import Queue
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

import app.graph_orchestrator as orchestrator
from app.auth import hash_password
from app.db import connect
from app.graph_orchestrator import _config, _review_lock, _view, build_review_graph
from app.main import create_app


def app_with_graph(path):
    return create_app(path, seed_demo=True)


def login(client, username, password=None):
    response = client.post('/api/auth/login', json={
        'username': username, 'password': password or f'{username}-demo-only',
    })
    assert response.status_code == 200


def test_graph_uses_bounded_model_tools_and_saves_only_trace(tmp_path, monkeypatch):
    path = tmp_path / 'model-graph.sqlite3'
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-secret')
    replies = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'call_1', 'type': 'function',
            'function': {'name': 'job_requirements', 'arguments': '{}'},
        }]},
        {'role': 'assistant', 'content': 'Untrusted output with a private claim'},
    ]
    monkeypatch.setattr('app.agent._request_model', lambda messages, key: replies.pop(0))
    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'resume.docx', 'PRIVATE RESUME')""", (candidate,)).lastrowid
        response = client.post('/api/graph-reviews', json={'application_id': application})
        assert response.status_code == 201
        result = response.json()
        assert result['awaiting_review']
        assert result['state']['agent_review'] == {'status': 'ok', 'tool_trace': ['job_requirements']}
        assert 'Untrusted output' not in str(result) and 'PRIVATE RESUME' not in str(result)
        saved = client.get(f"/api/graph-reviews/{result['id']}").json()
        assert saved['state']['agent_review'] == result['state']['agent_review']
        assert client.post(f"/api/graph-reviews/{result['id']}/resume", json={'reviewed': True}).status_code == 200


def test_graph_removes_partial_state_if_session_revoked_during_model_call(tmp_path, monkeypatch):
    path = tmp_path / 'revoked-graph.sqlite3'
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-secret')
    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'resume.docx', 'synthetic')""", (candidate,)).lastrowid

        def revoke(messages, key):
            with connect(path) as db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (hr,))
            return {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'job_requirements', 'arguments': '{}'},
            }]}

        monkeypatch.setattr('app.agent._request_model', revoke)
        response = client.post('/api/graph-reviews', json={'application_id': application})
        assert response.status_code == 401
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM checkpoints').fetchone()[0] == 0


def test_legacy_policy_checkpoint_is_redacted_from_response():
    legacy = SimpleNamespace(values={
        'application_id': 1, 'policy_question': 'private question',
        'policy': {'answer': 'revoked text', 'sources': [{'title': 'private title'}]},
        'matching': {'required': [{'skill': 'Python', 'mentioned': True,
                                   'evidence': ['private resume excerpt']}], 'preferred': []},
    }, next=('human_review',))
    view = _view(legacy)
    assert view['state']['policy'] == {'status': 'unavailable'}
    assert 'private question' not in str(view)
    assert 'revoked text' not in str(view)
    assert 'private title' not in str(view)
    assert 'private resume excerpt' not in str(view)


def test_review_lock_releases_after_process_crash(tmp_path):
    db_path = tmp_path / 'database.sqlite3'
    code = ("import sys,time; from app.graph_orchestrator import _review_lock; "
            "lock=_review_lock(sys.argv[1],sys.argv[2]); lock.__enter__(); "
            "print('locked',flush=True); time.sleep(120)")
    child = subprocess.Popen([sys.executable, '-u', '-c', code, str(db_path), 'thread'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = Queue()
    reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=45).strip() == 'locked'
        with pytest.raises(HTTPException) as error, _review_lock(db_path, 'thread'):
            pass
        assert error.value.status_code == 409
        child.kill()
        child.wait(timeout=10)
        deadline = time.monotonic() + 5
        while True:
            try:
                with _review_lock(db_path, 'thread'):
                    break
            except HTTPException:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()


def test_failed_start_removes_owner_and_partial_checkpoints(tmp_path, monkeypatch):
    path = tmp_path / 'failed.sqlite3'
    with TestClient(app_with_graph(path), raise_server_exceptions=False) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            application = db.execute("""INSERT INTO applications (candidate_id, job_id, filename, resume_text)
                VALUES (?, 'job-backend-python', 'resume.docx', 'Python')""", (candidate,)).lastrowid
        def fail(*args, **kwargs):
            raise RuntimeError('injected policy failure')
        monkeypatch.setattr(orchestrator, 'run_policy_agent', fail)
        assert client.post('/api/graph-reviews', json={
            'application_id': application, 'policy_question': 'trigger failure',
        }).status_code == 500
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM checkpoints').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM writes').fetchone()[0] == 0


def test_graph_policy_rechecks_source_before_first_response(tmp_path, monkeypatch):
    path = tmp_path / 'policy-race.sqlite3'
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    original = orchestrator.run_policy_agent
    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'resume.docx', 'synthetic')""", (candidate,)).lastrowid
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Quasar leave', 'audience': 'hr', 'collection': 'handbook', 'text': 'Quasar leave is eleven days. private-marker',
        }).json()

        def revoke(*args, **kwargs):
            answer = original(*args, **kwargs)
            assert answer.answer.sources
            with connect(path) as db:
                db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc['id'],))
            return answer

        monkeypatch.setattr(orchestrator, 'run_policy_agent', revoke)
        response = client.post('/api/graph-reviews', json={
            'application_id': application, 'policy_question': 'quasar leave',
        })
        assert response.status_code == 201
        assert response.json()['state']['policy'] == {'status': 'unavailable'}
        assert 'private-marker' not in response.text


def test_graph_policy_remains_available_after_restart_and_is_hidden_on_change(tmp_path, monkeypatch):
    path = tmp_path / 'policy-restart.sqlite3'
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'resume.docx', 'synthetic')""", (candidate,)).lastrowid
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Quasar leave', 'audience': 'hr', 'collection': 'handbook', 'text': 'Quasar leave is eleven days. private-marker',
        }).json()
        created = client.post('/api/graph-reviews', json={
            'application_id': application, 'policy_question': 'quasar leave',
        }).json()
        assert created['state']['policy']['status'] == 'answer'
        thread = created['id']

    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        restored = client.get(f'/api/graph-reviews/{thread}')
        assert restored.status_code == 200
        assert 'private-marker' in restored.json()['state']['policy']['answer']
        with connect(path) as db:
            db.execute('UPDATE knowledge_documents SET text=? WHERE id=?', ('Changed policy.', doc['id']))
        resumed = client.post(f'/api/graph-reviews/{thread}/resume', json={'reviewed': True})
        assert resumed.status_code == 200
        assert resumed.json()['state']['policy'] == {'status': 'unavailable'}
        assert 'private-marker' not in resumed.text


def test_concurrent_resume_has_exactly_one_winner(tmp_path, monkeypatch):
    path = tmp_path / 'concurrent.sqlite3'
    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            application = db.execute("""INSERT INTO applications (candidate_id, job_id, filename, resume_text)
                VALUES (?, 'job-backend-python', 'resume.docx', 'Python')""", (candidate,)).lastrowid
        created = client.post('/api/graph-reviews', json={'application_id': application})
        assert created.status_code == 201
        endpoint = f"/api/graph-reviews/{created.json()['id']}/resume"
        entered, release = threading.Event(), threading.Event()
        original = orchestrator.build_review_graph

        class SlowGraph:
            def __init__(self, graph):
                self.graph = graph

            def get_state(self, config):
                return self.graph.get_state(config)

            def invoke(self, value, config):
                entered.set()
                assert release.wait(timeout=15)
                return self.graph.invoke(value, config)

        monkeypatch.setattr(orchestrator, 'build_review_graph',
                            lambda *args, **kwargs: SlowGraph(original(*args, **kwargs)))
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(client.post, endpoint, json={'reviewed': True})
            try:
                assert entered.wait(timeout=10)
                second = client.post(endpoint, json={'reviewed': False})
                assert second.status_code == 409
            finally:
                release.set()
            assert first.result(timeout=15).status_code == 200
        assert client.get(f"/api/graph-reviews/{created.json()['id']}").json()['state']['reviewed'] is True


def test_scheduler_respects_cancellations_conflicts_and_application_status(tmp_path):
    path = tmp_path / 'availability.sqlite3'
    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        with connect(path) as db:
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()['id']
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            other = db.execute("INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                               ('othercandidate', 'Other', 'candidate', hash_password('unused-password'))).lastrowid
            def application(owner):
                return db.execute("""INSERT INTO applications (candidate_id, job_id, filename, resume_text, status)
                    VALUES (?, 'job-backend-python', 'resume.docx', 'Python', 'advanced')""", (owner,)).lastrowid
            target, candidate_other, other_application = application(candidate), application(candidate), application(other)
            def interval(day):
                return (f'2099-01-{day:02d}T10:00:00.000000Z', f'2099-01-{day:02d}T11:00:00.000000Z')
            def interview(app_id, owner, day, name, status):
                start, end = interval(day)
                return db.execute("""INSERT INTO interviews (application_id, candidate_id, start_at, end_at,
                    interviewer, interviewer_key, status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (app_id, owner, start, end, name, name.casefold(), status, hr)).lastrowid
            def slot(day, name, interview_id=None):
                start, end = interval(day)
                return db.execute("""INSERT INTO interview_slots (start_at, end_at, interviewer,
                    interviewer_key, created_by, interview_id) VALUES (?, ?, ?, ?, ?, ?)""",
                    (start, end, name, name.casefold(), hr, interview_id)).lastrowid
            released = slot(1, 'Alice', interview(other_application, other, 1, 'Alice', 'cancelled'))
            slot(2, 'Bob')
            interview(other_application, other, 2, 'Bob', 'scheduled')
            slot(3, 'Cara')
            interview(candidate_other, candidate, 3, 'Someone else', 'scheduled')
            slot(4, 'Dave', interview(other_application, other, 4, 'Dave', 'scheduled'))
            available = slot(5, 'Eve')
            withdrawn = slot(6, 'Frank')

        assert client.post(f'/api/slots/{withdrawn}/withdraw').status_code == 200

        def schedule():
            response = client.post('/api/graph-reviews', json={'application_id': target})
            assert response.status_code == 201, response.text
            return response.json()

        first = schedule()
        result = first['state']['scheduling']
        assert result['eligible'] is True
        assert [entry['id'] for entry in result['available_slots']] == [released, available]
        assert client.post(f'/api/slots/{available}/withdraw').status_code == 200
        refreshed = client.get(f"/api/graph-reviews/{first['id']}").json()['state']['scheduling']
        assert [entry['id'] for entry in refreshed['available_slots']] == [released]
        with connect(path) as db:
            db.execute("UPDATE applications SET status='rejected' WHERE id=?", (target,))
        current = client.get(f"/api/graph-reviews/{first['id']}").json()['state']
        assert current['status']['application_status'] == 'rejected'
        assert current['scheduling']['available_slots'] == []
        resumed = client.post(f"/api/graph-reviews/{first['id']}/resume", json={'reviewed': True}).json()['state']
        assert resumed['status']['application_status'] == 'rejected'
        assert resumed['scheduling']['available_slots'] == []
        assert schedule()['state']['scheduling']['available_slots'] == []
        with connect(path) as db:
            db.execute("UPDATE applications SET status='advanced' WHERE id=?", (target,))
            start, end = '2099-01-06T10:00:00.000000Z', '2099-01-06T11:00:00.000000Z'
            db.execute("""INSERT INTO interviews (application_id, candidate_id, start_at, end_at, interviewer,
                interviewer_key, status, created_by) VALUES (?, ?, ?, ?, 'Test', 'test', 'scheduled', ?)""",
                (target, candidate, start, end, hr))
        assert schedule()['state']['scheduling']['available_slots'] == []
        current = client.get(f"/api/graph-reviews/{first['id']}").json()['state']
        assert current['status']['application_status'] == 'advanced'
        assert current['scheduling']['available_slots'] == []


def test_graph_review_persists_and_requires_owner_hr(tmp_path):
    path = tmp_path / 'graph.sqlite3'
    with TestClient(app_with_graph(path)) as client:
        login(client, 'candidate')
        document = Document()
        document.add_paragraph('Skills')
        document.add_paragraph('Built Python services and SQL databases. private-cv-marker')
        file = BytesIO()
        document.save(file)
        application = client.post('/api/applications', params={
            'job_id': 'job-backend-python', 'filename': 'resume.docx',
        }, content=file.getvalue()).json()
        request = {'application_id': application['id'], 'policy_question': 'what is quasar sabbatical?'}
        assert client.post('/api/graph-reviews', json=request).status_code == 403

        login(client, 'hr')
        document = client.post('/api/knowledge/documents/text', json={
            'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
            'text': 'Quasar sabbatical is eleven days. confidential-policy-marker',
        })
        assert document.status_code == 201
        audit_before = client.get('/api/audit').json()
        created = client.post('/api/graph-reviews', json=request)
        assert created.status_code == 201, created.text
        body = created.json()
        thread_id = body['id']
        assert body['awaiting_review'] is True
        assert body['state']['plan'] == ['parser', 'matching', 'policy', 'scheduler', 'status', 'agent_review', 'human_review']
        assert body['state']['parsed']['text_available'] is True
        assert body['state']['parsed']['sections']['skills'] == 'mentioned'
        assert body['state']['parsed']['sections']['education'] == 'unknown'
        assert 'private-cv-marker' not in str(body)
        assert body['state']['matching']['method']
        assert body['state']['policy']['status'] == 'answer'
        assert 'confidential-policy-marker' in body['state']['policy']['answer']
        assert body['state']['policy']['sources'][0]['title'] == 'Quasar policy'
        assert request['policy_question'] not in str(body)
        assert body['state']['status']['application_status'] == 'submitted'
        with SqliteSaver.from_conn_string(str(path)) as saver:
            graph = build_review_graph(path, saver)
            history = list(graph.get_state_history(_config(thread_id)))
            assert len(history) > 1
            assert request['policy_question'] not in str(history)
            assert 'confidential-policy-marker' not in str(history)
            assert 'private-cv-marker' not in str(history)

        assert client.post(f"/api/knowledge/documents/{document.json()['id']}/deactivate").status_code == 200
        redacted = client.get(f'/api/graph-reviews/{thread_id}')
        assert redacted.json()['state']['policy'] == {'status': 'unavailable'}
        assert 'confidential-policy-marker' not in redacted.text

        # A live session must not retain HR authorization after the role changes.
        with connect(path) as db:
            db.execute("UPDATE users SET role = 'employee' WHERE username = 'hr'")
        assert client.get(f'/api/graph-reviews/{thread_id}').status_code == 403
        assert client.post(f'/api/graph-reviews/{thread_id}/resume', json={'reviewed': True}).status_code == 403
        with connect(path) as db:
            db.execute("UPDATE users SET role = 'hr' WHERE username = 'hr'")

        with connect(path) as db:
            db.execute("INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                       ('otherhr', 'Other HR', 'hr', hash_password('other-password')))
        login(client, 'otherhr', 'other-password')
        assert client.get(f'/api/graph-reviews/{thread_id}').status_code == 404
        assert client.post(f'/api/graph-reviews/{thread_id}/resume', json={'reviewed': True}).status_code == 404
        login(client, 'candidate')
        assert client.get(f'/api/graph-reviews/{thread_id}').status_code == 403

    # Fresh app and checkpointer: the pending interrupt survives process restart.
    with TestClient(app_with_graph(path)) as client:
        login(client, 'hr')
        pending = client.get(f'/api/graph-reviews/{thread_id}')
        assert pending.status_code == 200
        assert pending.json()['awaiting_review'] is True
        assert 'confidential-policy-marker' not in pending.text
        assert pending.json()['state']['policy'] == {'status': 'unavailable'}
        completed = client.post(f'/api/graph-reviews/{thread_id}/resume', json={'reviewed': True})
        assert completed.status_code == 200, completed.text
        assert completed.json()['state']['reviewed'] is True
        assert completed.json()['awaiting_review'] is False
        assert 'confidential-policy-marker' not in completed.text
        assert completed.json()['state']['policy'] == {'status': 'unavailable'}
        assert client.post(f'/api/graph-reviews/{thread_id}/resume', json={'reviewed': True}).status_code == 409
        assert client.get(f"/api/applications/{application['id']}").json()['status'] == 'submitted'
        assert client.get('/api/audit').json() == audit_before
        with connect(path) as db:
            db.execute("UPDATE users SET role = 'employee' WHERE username = 'hr'")
        assert client.get(f'/api/graph-reviews/{thread_id}').status_code == 403
