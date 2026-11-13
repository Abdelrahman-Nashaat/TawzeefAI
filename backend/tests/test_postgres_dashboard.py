"""HR dashboard counts persisted records in the PostgreSQL-only demo."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_dashboard_uses_recorded_pipeline_and_hire_times(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        assert candidate.get('/api/dashboard/jobs').status_code == 401
        login(candidate, 'candidate')
        assert candidate.get('/api/dashboard/jobs').status_code == 403
        jobs = candidate.get('/api/jobs').json()
        job = jobs[0]['id']
        for _ in range(2):
            r = candidate.post('/api/applications', params={'job_id':job,'filename':'cv.docx'}, content=resume_bytes())
            assert r.status_code == 201
        login(hr, 'hr')
        assert hr.post('/api/applications/1/screen').status_code == 200
        assert hr.post('/api/applications/1/screen').status_code == 200
        with db_for(mode) as db:
            db.execute("UPDATE applications SET status='hired',created_at='2026-01-01T00:00:00Z' WHERE id=1")
            db.execute("INSERT INTO audit_events(application_id,actor_id,action,details,created_at) SELECT 1,id,'decision',%s,'2026-01-04T00:00:00Z' FROM users WHERE username='hr'", ('{"decision":"hire"}',))
        with db_for(mode) as db:
            for action, details in [('decision', '{"decision":"advance"}'), ('interview_completed', '{}'),
                                    ('interview_completed', '{}'), ('offer_presented', '{}'),
                                    ('decision', 'invalid JSON')]:
                db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) SELECT 1,id,%s,%s FROM users WHERE username='hr'", (action, details))
            db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) SELECT 2,id,'decision',%s FROM users WHERE username='hr'", ('{"decision":"reject"}',))
        response = hr.get('/api/dashboard/jobs')
        assert response.status_code == 200, response.text
        data = response.json()
        item = next(row for row in data['jobs'] if row['job_id'] == job)
        assert item['applications_total'] == 2
        assert item['screening_events'] == 2
        assert item['pipeline'] == [{'status':'hired','count':1},{'status':'submitted','count':1}]
        assert item['milestones'] == {'screened': 1, 'advanced': 1, 'interview_completed': 1,
                                      'offer_presented': 1, 'hired': 1, 'rejected': 1}
        assert item['time_to_hire'] == {'average_days':3.0,'count':1}
        assert all(row['time_to_hire']['average_days'] is None for row in data['jobs'] if row['job_id'] != job)
        assert 'manual baseline' in data['definition']
        assert 'resume_text' not in response.text
