"""Negative or future-learning statements are not evidence of an existing skill."""
import pytest
from fastapi.testclient import TestClient

from app.blind import BlindText
from app.db import connect
from app.main import create_app
from app.recruitment import _score_blind


@pytest.mark.parametrize('text', [
    'I don\u02bct know Python.',
    'I don\u2018t know Python.',
    'I plan to learn Python.',
    'I am planning to learn Python.',
    'I hope to learn Python.',
    'I intend to learn Python.',
    'I needn\u02bct use Python.',
    'I will learn Python next year.',
    'I am going to learn Python.',
    '\u0648\u064e\u0644\u064e\u0627 \u0623\u0639\u0631\u0641 Python.',
])
def test_negative_and_future_learning_are_not_skill_evidence(text):
    job = {'required_skills': ['Python'], 'preferred_skills': []}
    result = _score_blind(job, BlindText(text=text, masked=False))
    assert result['score'] == 0
    assert result['required'][0]['mentioned'] is False
    assert result['required'][0]['evidence'] == []


@pytest.mark.parametrize('text', [
    'Built Python services.',
    'Built Python systems; planning to improve latency.',
    'Built Python services. I plan to learn Rust.',
    '\u0623\u0639\u0645\u0644 \u0628\u0627\u0633\u062a\u062e\u062f\u0627\u0645 Python.',
    '\u0628\u0646\u064a\u062a \u0645\u064e\u062b\u064e\u0644\u064e\u0627 Python services.',
])
def test_positive_skill_evidence_is_retained(text):
    job = {'required_skills': ['Python'], 'preferred_skills': []}
    result = _score_blind(job, BlindText(text=text, masked=False))
    assert result['score'] == 100
    assert result['required'][0]['mentioned'] is True
    assert any('Python' in item for item in result['required'][0]['evidence'])


@pytest.mark.parametrize('path', ['direct', 'graph'])
def test_api_screening_does_not_score_a_negative_contraction(tmp_path, path):
    app = create_app(tmp_path / 'screen.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.pdf',?,'submitted')""", (candidate, 'I don\u02bct know Python.')).lastrowid
        if path == 'direct':
            response = client.post(f'/api/applications/{application}/screen')
            assert response.status_code == 200
            result = response.json()
        else:
            response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'screen_application'})
            assert response.status_code == 201
            result = response.json()['state']['screening_preview']['score']
        python = next(row for row in result['required'] if row['skill'].casefold() == 'python')
        assert python['mentioned'] is False
        assert python['evidence'] == []
        with connect(app.state.database_path) as db:
            assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
            assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
