import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.jobs_admin import initialize_jobs_admin
from app.jobs_admin import router as jobs_admin_router
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    app = create_app(database_path=path, seed_demo=True)
    app.include_router(jobs_admin_router)
    with TestClient(app) as client:
        initialize_jobs_admin(path)
        yield client


def login(client, role):
    response = client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'})
    assert response.status_code == 200


def job(**overrides):
    return {
        'id': 'backend-engineer', 'title': ' Backend Engineer ', 'location': ' Cairo ',
        'required_skills': [' Python ', 'SQL'], 'preferred_skills': [],
        'responsibilities': [' Design APIs '], **overrides,
    }


def test_creation_is_hr_only_persistent_visible_and_audited(client):
    assert client.post('/api/jobs', json=job()).status_code == 401
    for role in ('candidate', 'employee'):
        login(client, role)
        assert client.post('/api/jobs', json=job()).status_code == 403
    login(client, 'hr')
    created = client.post('/api/jobs', json=job())
    assert created.status_code == 201
    assert created.json() == job(title='Backend Engineer', location='Cairo', required_skills=['Python', 'SQL'], responsibilities=['Design APIs'])
    assert client.post('/api/jobs', json=job()).status_code == 409
    login(client, 'candidate')
    assert any(j['id'] == 'backend-engineer' for j in client.get('/api/jobs').json())
    with connect(client.app.state.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE id='backend-engineer'").fetchone()[0] == 1
        rows = db.execute('SELECT * FROM job_audit_events').fetchall()
        assert len(rows) == 1 and rows[0]['job_id'] == 'backend-engineer' and rows[0]['action'] == 'created'


@pytest.mark.parametrize('payload', [
    job(id='Uppercase'), job(id='../bad'), job(id='x--y'), job(id='é'),
    job(id='a' * 65), job(title='   '), job(location='x' * 201),
    job(required_skills=[]), job(required_skills=[' ']), job(required_skills=['a' * 201]),
    job(required_skills=['A'] * 51), job(preferred_skills=['']), job(responsibilities=[]),
    job(responsibilities=['bad\x00value']), job(extra='unexpected'),
])
def test_invalid_input_does_not_write_jobs_or_audit(client, payload):
    login(client, 'hr')
    assert client.post('/api/jobs', json=payload).status_code == 422
    with connect(client.app.state.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM job_audit_events').fetchone()[0] == 0


def test_audit_failure_rolls_back_job_creation(client):
    login(client, 'hr')
    with connect(client.app.state.database_path) as db:
        db.execute("CREATE TRIGGER fail_job_audit BEFORE INSERT ON job_audit_events BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        client.post('/api/jobs', json=job())
    with connect(client.app.state.database_path) as db:
        assert db.execute("SELECT 1 FROM jobs WHERE id = 'backend-engineer'").fetchone() is None


def test_duplicate_creation_is_atomic(client):
    login(client, 'hr')
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: client.post('/api/jobs', json=job()).status_code, range(2)))
    assert sorted(statuses) == [201, 409]
    with connect(client.app.state.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM job_audit_events').fetchone()[0] == 1
