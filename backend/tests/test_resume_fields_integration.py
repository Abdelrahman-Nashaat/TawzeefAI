from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def login(client, username):
    assert client.post('/api/auth/login', json={
        'username': username, 'password': f'{username}-demo-only',
    }).status_code == 200


def test_redaction_expansion_returns_limit_without_breaking_review(tmp_path):
    path = tmp_path / 'expanded.sqlite3'
    source = ('Name: x\n' * 24999).rstrip()
    assert len(source) < 200_000
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'example.docx', ?)""", (candidate, source)).lastrowid
        fields = client.get(f'/api/applications/{application}/resume/fields')
        assert fields.status_code == 200
        assert fields.json()['status'] == 'limit_exceeded'
        assert 'sections' not in fields.json()
        review = client.post('/api/graph-reviews', json={'application_id': application})
        assert review.status_code == 201, review.text
        assert review.json()['state']['resume_fields']['status'] == 'limit_exceeded'
        assert 'sections' not in review.json()['state']['resume_fields']
        assert review.json()['awaiting_review']
        login(client, 'candidate')
        assert client.get(f'/api/applications/{application}/resume/fields').status_code == 403


def test_fields_hr_graph_rechecks_source_and_checkpoint_contains_no_text(tmp_path):
    path = tmp_path / 'fields.sqlite3'
    source = 'Experience\nRole: Synthetic Field Analyst\nCompany: Example Company\n'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'example.docx', ?)""", (candidate, source)).lastrowid
        endpoint = f'/api/applications/{application}/resume/fields'
        fields = client.get(endpoint)
        assert fields.status_code == 200
        role = fields.json()['sections']['experience']['records'][0]['fields']['role']
        assert role['value'] == 'Synthetic Field Analyst'
        assert source[role['start']:role['end']] == role['value']
        review = client.post('/api/graph-reviews', json={'application_id': application})
        assert review.status_code == 201
        data = review.json()
        assert data['state']['resume_fields']['status'] == 'current'
        assert data['state']['resume_fields']['sections']['experience']['records'][0]['fields']['role']['value'] == role['value']
        with connect(path) as db:
            checkpoints = db.execute('SELECT checkpoint FROM checkpoints').fetchall()
            writes = db.execute('SELECT value FROM writes').fetchall()
            assert all(b'Synthetic Field Analyst' not in bytes(row[0]) for row in checkpoints + writes if row[0] is not None)
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', (source + 'Projects\nProject: New\n', application))
        refreshed = client.get(f"/api/graph-reviews/{data['id']}")
        assert refreshed.status_code == 200
        assert refreshed.json()['state']['resume_fields']['status'] == 'stale'
        assert 'Synthetic Field Analyst' not in str(refreshed.json()['state'].get('resume_fields'))
        assert 'parsed' not in refreshed.json()['state']
        login(client, 'candidate')
        assert client.get(endpoint).status_code == 403
        assert client.get(f"/api/graph-reviews/{data['id']}").status_code == 403
