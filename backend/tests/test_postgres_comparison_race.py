"""Check expired HR sessions cannot retrieve delayed comparison results."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import wait_for_lock

from app.main import create_app


def test_comparison_expiry_while_waiting(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    app = create_app(seed_demo=True)
    with TestClient(app) as a, TestClient(app) as b, TestClient(app) as hr:
        login(a, 'candidate')
        login(b, 'candidate2')
        login(hr, 'hr')
        job = a.get('/api/jobs').json()[0]['id']
        ids = [client.post('/api/applications', params={'job_id': job, 'filename': 'r.docx'}, content=resume_bytes()).json()['id'] for client in (a, b)]
        names = ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']
        for app_id in ids:
            source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
            start = next(i for i, c in enumerate(source['resume_text']) if not c.isspace())
            items = [{'category': name, 'score': None} for name in names]
            items[0] = {'category': names[0], 'score': 50, 'start': start, 'end': start + 1}
            assert hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).status_code == 200
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('UPDATE applications SET status=status WHERE id=%s', (ids[1],))
            future = pool.submit(hr.post, '/api/hr/rubric-comparison', json={'application_ids': ids})
            wait_for_lock(mode, 'transactionid')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
