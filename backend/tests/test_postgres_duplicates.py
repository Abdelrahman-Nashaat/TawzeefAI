"""Duplicate review compares extracted text without automating decisions."""

import io
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import wait_for_lock

from app.main import create_app


def make_resume(text):
    document = Document()
    document.add_paragraph(text)
    result = io.BytesIO()
    document.save(result)
    return result.getvalue()


def test_duplicate_review_end_to_end_and_role_boundaries(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as other, TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as employee:
        login(candidate, 'candidate')
        login(other, 'candidate2')
        login(hr, 'hr')
        login(employee, 'employee')
        jobs = candidate.get('/api/jobs').json()
        def apply(client, document, job):
            result = client.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=document)
            assert result.status_code == 201, result.text
            return result.json()['id']
        source = apply(candidate, resume_bytes(), jobs[0]['id'])
        duplicate = apply(other, resume_bytes(), jobs[-1]['id'])
        different = apply(candidate, make_resume('A different synthetic resume.'), jobs[0]['id'])
        endpoint = f'/api/applications/{source}/duplicates'
        assert candidate.get(endpoint).status_code == 403
        assert employee.get(endpoint).status_code == 403
        assert hr.get('/api/applications/99999999/duplicates').status_code == 404
        result = hr.get(endpoint)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['application_id'] == source
        assert data['match_type'] == 'exact_extracted_text'
        assert data['truncated'] is False
        assert [match['application_id'] for match in data['matches']] == [duplicate]
        assert set(data['matches'][0]) == {'application_id', 'candidate_id', 'job_id', 'status', 'created_at'}
        assert hr.get(f'/api/applications/{different}/duplicates').json()['matches'] == []
        with db_for(mode) as db:
            assert all(row['status'] == 'submitted' for row in db.execute('SELECT status FROM applications'))
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        assert hr.get(endpoint).json() == data


def test_duplicate_results_are_bounded_and_empty_text_is_not_matched(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        with db_for(mode) as db:
            candidate_id = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            job_id = db.execute('SELECT id FROM jobs LIMIT 1').fetchone()['id']
            ids = []
            for text in ['same'] * 53 + [' ', ' ']:
                ids.append(db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                    VALUES (%s,%s,'cv.docx',%s) RETURNING id''', (candidate_id, job_id, text)).fetchone()['id'])
        result = hr.get(f'/api/applications/{ids[0]}/duplicates').json()
        assert result['truncated'] is True and len(result['matches']) == 50
        assert result['matches'][0]['application_id'] == ids[52]
        assert hr.get(f'/api/applications/{ids[53]}/duplicates').json()['matches'] == []


def test_session_expiry_during_comparison_returns_no_data(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        created = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes())
        application_id = created.json()['id']
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('SELECT id FROM applications WHERE id=%s FOR UPDATE', (application_id,))
            future = pool.submit(hr.get, f'/api/applications/{application_id}/duplicates')
            wait_for_lock(mode, 'transactionid')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
