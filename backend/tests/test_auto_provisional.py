from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from time import monotonic, sleep

from docx import Document
from fastapi.testclient import TestClient

from app import auto_provisional
from app.db import connect
from app.main import create_app

JOB = 'job-backend-python'
QUEUE = f'/api/jobs/{JOB}/provisional-queue'


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def submit(client, text='Built Python applications and SQL databases.'):
    doc = Document()
    doc.add_paragraph(text)
    buffer = BytesIO()
    doc.save(buffer)
    response = client.post('/api/applications', params={'job_id': JOB, 'filename': 'synthetic.docx'}, content=buffer.getvalue())
    assert response.status_code == 201
    return response.json()['id']


def test_worker_scores_without_advancing_and_identifies_system_actor(tmp_path):
    app = create_app(tmp_path / 'worker.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        application = submit(client)
        login(client, 'hr')
        assert client.get(QUEUE).json()['items'][0]['recommendation'] == 'pending'
        assert auto_provisional.run_one(app.state.database_path)
        item = client.get(QUEUE).json()['items'][0]
        assert item['origin'] == 'system_worker'
        assert item['automatic_state'] == 'done'
        assert item['recommendation'] == 'mask_review_required'
        assert item['score'] is not None and item['current']
        history = client.get(f'/api/applications/{application}/provisional-history').json()
        assert history['automatic_results'][0]['actor'] == 'system_worker'
        assert 'Python applications' not in str(history)
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT status,score_json FROM applications WHERE id=?', (application,)).fetchone()[:] == ('submitted', None)
        assert not auto_provisional.run_one(app.state.database_path)


def test_expired_lease_is_recovered_and_old_worker_is_fenced(tmp_path):
    app = create_app(tmp_path / 'lease.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        submit(client)
        first = auto_provisional.claim(app.state.database_path)
        with connect(app.state.database_path) as db:
            db.execute('UPDATE auto_provisional_jobs SET lease_until=0 WHERE snapshot_id=?', (first[0],))
        second = auto_provisional.claim(app.state.database_path)
        assert second[0] == first[0] and second[1] != first[1]
        auto_provisional.process_claim(app.state.database_path, *first)
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT state,lease_token FROM auto_provisional_jobs').fetchone()[:] == ('running', second[1])
        auto_provisional.process_claim(app.state.database_path, *second)
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT state,attempts FROM auto_provisional_jobs').fetchone()[:] == ('done', 2)
        auto_provisional.initialize_auto_screen(app.state.database_path)
        assert not auto_provisional.run_one(app.state.database_path)


def test_work_limit_and_changed_source_never_create_stale_score(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'bounds.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        first = submit(client, 'x' * 4100)
        second = submit(client)
        assert auto_provisional.run_one(app.state.database_path)
        original = auto_provisional.score_masked_resume
        def change(job, source):
            with connect(app.state.database_path) as db:
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('changed', second))
            return original(job, source)
        monkeypatch.setattr(auto_provisional, 'score_masked_resume', change)
        assert auto_provisional.run_one(app.state.database_path)
        with connect(app.state.database_path) as db:
            states = db.execute('SELECT s.application_id,j.state,j.score FROM auto_provisional_jobs j JOIN provisional_snapshots s ON s.id=j.snapshot_id ORDER BY s.id').fetchall()
            assert [(r['application_id'],r['state'],r['score']) for r in states] == [(first,'manual',None),(second,'superseded',None)]
        login(client, 'hr')
        items = client.get(QUEUE).json()['items']
        assert [i['automatic_state'] for i in items] == ['manual', 'superseded']
        assert all(i['score'] is None for i in items)


def test_hr_refresh_wins_during_background_scoring(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'hr-race.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        application = submit(client)
        login(client, 'hr')
        pending = client.get(QUEUE).json()['items'][0]['snapshot_id']
        original = auto_provisional.score_masked_resume
        def concurrent_hr(job, source):
            response = client.post(f'/api/applications/{application}/provisional-screening', json={'expected_snapshot_id': pending})
            assert response.status_code == 201
            return original(job, source)
        monkeypatch.setattr(auto_provisional, 'score_masked_resume', concurrent_hr)
        assert auto_provisional.run_one(app.state.database_path)
        item = client.get(QUEUE).json()['items'][0]
        assert item['origin'] == 'hr'
        assert item['snapshot_id'] != pending
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT state FROM auto_provisional_jobs WHERE snapshot_id=?', (pending,)).fetchone()[0] == 'superseded'


def test_simultaneous_claims_only_one_worker_and_opt_in_lifecycle(tmp_path, monkeypatch):
    path = tmp_path / 'workers.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        submit(client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: auto_provisional.claim(path), range(2)))
        assert sum(claim is not None for claim in claims) == 1
        with connect(path) as db:
            db.execute("UPDATE auto_provisional_jobs SET lease_until=0 WHERE state='running'")
    monkeypatch.setenv('TAWZEEF_AUTO_PROVISIONAL', '1')
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        deadline = monotonic() + 15
        while monotonic() < deadline:
            data = client.get(QUEUE).json()
            if data['worker']['recent_heartbeat'] and data['items'][0]['automatic_state'] == 'done':
                break
            sleep(0.05)
        assert data['worker']['recent_heartbeat']
        assert data['items'][0]['origin'] == 'system_worker'
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM auto_provisional_workers').fetchone()[0] == 0


def test_worker_concurrency_cap_and_bounded_retry(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'concurrency.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        for _ in range(3):
            submit(client)
        with ThreadPoolExecutor(max_workers=3) as pool:
            claims = list(pool.map(lambda _: auto_provisional.claim(app.state.database_path), range(3)))
        assert sum(claim is not None for claim in claims) == 2
        with connect(app.state.database_path) as db:
            db.execute("UPDATE auto_provisional_jobs SET lease_until=0 WHERE state='running'")
        def failure(*args):
            raise ValueError('synthetic failure')
        monkeypatch.setattr(auto_provisional, 'score_masked_resume', failure)
        for _ in range(8):
            auto_provisional.run_one(app.state.database_path)
        with connect(app.state.database_path) as db:
            rows = db.execute('SELECT state,attempts FROM auto_provisional_jobs ORDER BY snapshot_id').fetchall()
            assert all(row['state'] == 'failed' and row['attempts'] == 3 for row in rows)


def test_legacy_pending_backfill_is_incremental(tmp_path):
    from app.provisional_queue import create_pending_snapshot

    app = create_app(tmp_path / 'legacy.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        application = submit(client)
        with connect(app.state.database_path) as db:
            db.execute('DROP TRIGGER auto_provisional_enqueue')
            db.execute("UPDATE auto_provisional_jobs SET state='superseded'")
            actor = db.execute('SELECT candidate_id FROM applications WHERE id=?', (application,)).fetchone()[0]
            for _ in range(101):
                last = create_pending_snapshot(db, application, JOB, actor)
        auto_provisional.initialize_auto_screen(app.state.database_path)
        assert not auto_provisional.run_one(app.state.database_path)
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT after_id FROM auto_provisional_backfill').fetchone()[0] < last
        assert auto_provisional.run_one(app.state.database_path)
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT state FROM auto_provisional_jobs WHERE snapshot_id=?', (last,)).fetchone()[0] == 'done'
