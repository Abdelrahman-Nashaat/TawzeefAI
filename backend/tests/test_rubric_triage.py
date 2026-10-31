from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def test_triage_is_human_review_only_and_marks_stale(tmp_path):
    app = create_app(tmp_path / 'triage.sqlite3', seed_demo=True)
    path = '/api/jobs/job-backend-python/rubric/triage'
    settings = path + '-threshold'
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        login(client, 'candidate')
        assert client.get(path).status_code == 403
        assert client.put(settings, json={'threshold': 50, 'expected_version': 0}).status_code == 403
        login(client, 'hr')
        assert client.get(settings).json() == {'threshold': None, 'version': 0}
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            ids = [db.execute('INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,?,?,?)',
                              (candidate, 'job-backend-python', 'synthetic.pdf', 'Python SQL')).lastrowid
                   for _ in range(4)]
        criteria = []
        for category in ('mandatory', 'preferred', 'experience', 'projects', 'education'):
            response = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
                'category': category, 'label': f'{category} criterion'})
            assert response.status_code == 201
            criteria.append(response.json()['criterion_ref'])
        for app_id, status in zip(ids[:3], ('met', 'missing', 'unknown'), strict=True):
            evidence = client.post(f'/api/applications/{app_id}/rubric/evidence', json={'start': 0, 'end': 6}).json()['evidence_ref']
            judgments = [{'criterion_ref': ref, 'status': status,
                          **({'evidence_refs': [evidence]} if status != 'unknown' else {}),
                          **({'absence_review_attested': True} if status == 'missing' else {})} for ref in criteria]
            assert client.post(f'/api/applications/{app_id}/rubric/assessment', json={'judgments': judgments}).status_code == 201
        result = client.get(path).json()
        assert [item['recommendation'] for item in result['items']] == [
            'threshold_not_configured', 'threshold_not_configured', 'threshold_not_configured', 'unknown']
        assert client.put(settings, json={'threshold': 100, 'expected_version': 0}).json() == {'threshold': 100, 'version': 1}
        assert client.put(settings, json={'threshold': 40, 'expected_version': 0}).status_code == 409
        assert client.put(settings, json={'threshold': True, 'expected_version': 1}).status_code == 422
        result = client.get(path).json()
        assert [item['recommendation'] for item in result['items']] == [
            'priority_review', 'needs_review', 'inconclusive', 'unknown']
        assert all(item['application_status'] == 'submitted' for item in result['items'])
        assert 'Python SQL' not in str(result)
        # Closed applications remain visible but must not enter the active
        # review queue, regardless of their current assessment or threshold.
        with connect(app.state.database_path) as db:
            db.execute("UPDATE applications SET status='hired' WHERE id=?", (ids[0],))
            db.execute("UPDATE applications SET status='rejected' WHERE id=?", (ids[1],))
        closed = client.get(path).json()['items']
        assert [item['recommendation'] for item in closed] == [
            'finalized', 'finalized', 'inconclusive', 'unknown']
        assert [item['application_status'] for item in closed[:2]] == ['hired', 'rejected']
        assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'New criterion'}).status_code == 201
        assert [item['recommendation'] for item in client.get(path).json()['items']] == [
            'finalized', 'finalized', 'unknown', 'unknown']
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT COUNT(*) FROM rubric_triage_audit').fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='decision'").fetchone()[0] == 0


def test_concurrent_threshold_updates_allow_only_one_writer(tmp_path):
    app = create_app(tmp_path / 'race.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        login(client, 'hr')
        endpoint = '/api/jobs/job-backend-python/rubric/triage-threshold'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda value: client.put(endpoint, json={
                'threshold': value, 'expected_version': 0}).status_code, [40, 60]))
        assert sorted(outcomes) == [200, 409]
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT COUNT(*) FROM rubric_triage_audit').fetchone()[0] == 1
