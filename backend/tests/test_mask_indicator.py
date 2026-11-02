"""Masking indicators distinguish reviewed text from text actually hidden."""

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.mark.parametrize(('source', 'spans', 'expected'), [
    ('Built Python and SQL services.', [], False),
    ('Julia Smith\nBuilt Python and SQL services.', [{'start': 0, 'end': 11}], True),
    ('Name: Julia Smith\nBuilt Python and SQL services.', [], True),
])
def test_review_and_actual_mask_are_distinct(tmp_path, source, spans, expected):
    app = create_app(tmp_path / 'mask.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={
            'username': 'hr', 'password': 'hr-demo-only',
        }).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.pdf', ?)''', (candidate, source)).lastrowid
        base = f'/api/applications/{application}'
        before = client.post(base + '/screen')
        assert before.status_code == 200
        assert before.json()['blind_screening']['masked'] == source.startswith('Name:')
        assert 'review_status' not in before.json()['blind_screening']
        review = client.get(base + '/resume/mask').json()
        result = client.put(base + '/resume/mask', json={
            'source_sha256': review['source_sha256'], 'expected_source_revision': review['source_revision'],
            'expected_revision': review['revision'], 'spans': spans,
        })
        assert result.status_code == 200
        after = client.post(base + '/screen')
        assert after.status_code == 200
        assert after.json()['blind_screening']['review_status'] == 'reviewed'
        assert after.json()['blind_screening']['masked'] is expected
