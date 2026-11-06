"""Compare independent HR judgments through the PostgreSQL public API."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_comparison_ranges_access_and_staleness(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as first, TestClient(app) as second, TestClient(app) as hr:
        login(first, 'candidate')
        login(second, 'candidate2')
        login(hr, 'hr')
        jobs = first.get('/api/jobs').json()
        ids = []
        for client in (first, second):
            response = client.post('/api/applications', params={'job_id': jobs[0]['id'], 'filename': 'resume.docx'}, content=resume_bytes())
            assert response.status_code == 201, response.text
            ids.append(response.json()['id'])
        endpoint = '/api/hr/rubric-comparison'
        payload = {'application_ids': ids}
        assert first.post(endpoint, json=payload).status_code == 403
        assert hr.post(endpoint, json=payload).status_code == 409
        categories = ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']
        for index, app_id in enumerate(ids):
            source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
            start = next(i for i, c in enumerate(source['resume_text']) if not c.isspace())
            items = [{'category': category, 'score': 100 if index == 0 else 0, 'start': start, 'end': start + 1} for category in categories]
            response = hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items})
            assert response.status_code == 200, response.text
        result = hr.post(endpoint, json=payload)
        assert result.status_code == 200, result.text
        assert [(item['lower'], item['upper']) for item in result.json()['reviews']] == [(100, 100), (0, 0)]
        assert result.json()['pairs'][0]['higher_range'] == ids[0]
        assert 'resume_text' not in result.text
        assert hr.post(endpoint, json={'application_ids': [ids[0], ids[0]]}).status_code == 422
        with db_for(mode) as db:
            db.execute('''INSERT INTO jobs(id,title,location,required_skills,preferred_skills,responsibilities)
                SELECT 'identical-role',title,location,required_skills,preferred_skills,responsibilities
                FROM jobs WHERE id=%s''', (jobs[0]['id'],))
            db.execute("UPDATE applications SET job_id='identical-role' WHERE id=%s", (ids[1],))
        assert hr.get(f'/api/applications/{ids[1]}/rubric-assessment').status_code == 409
        assert hr.post(endpoint, json=payload).status_code == 422


def test_overlapping_ranges_and_tampered_review(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as a, TestClient(app) as b, TestClient(app) as hr:
        login(a, 'candidate')
        login(b, 'candidate2')
        login(hr, 'hr')
        jobs = a.get('/api/jobs').json()
        ids = [client.post('/api/applications', params={'job_id': jobs[0]['id'], 'filename': 'r.docx'}, content=resume_bytes()).json()['id'] for client in (a, b)]
        categories = ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']
        for app_id in ids:
            source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
            start = next(i for i, c in enumerate(source['resume_text']) if not c.isspace())
            items = [{'category': name, 'score': None} for name in categories]
            items[0] = {'category': categories[0], 'score': 50, 'start': start, 'end': start + 1}
            assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).status_code == 200
        response = hr.post('/api/hr/rubric-comparison', json={'application_ids': ids})
        assert response.status_code == 200, response.text
        assert response.json()['pairs'][0]['higher_range'] is None
        with db_for(mode) as db:
            db.execute("UPDATE hr_rubric_history SET review_json='{}' WHERE application_id=%s", (ids[0],))
        assert hr.post('/api/hr/rubric-comparison', json={'application_ids': ids}).status_code == 409
