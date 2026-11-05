"""The Go programming language must not be inferred from ordinary verbs."""

import json

import pytest
from fastapi.testclient import TestClient

from app.blind import BlindText
from app.db import connect
from app.main import create_app
from app.recruitment import _score_blind


@pytest.mark.parametrize('text', [
    'I go to meetings and coordinate our team.',
    'Seeking to go beyond expectations.',
    'The team can go live after the release.',
    'I go home after work.',
    'Worked with go-to-market teams.',
    'Skills: help teams go faster and coordinate delivery',
])
def test_ordinary_go_is_not_language_evidence(text):
    result = _score_blind({'required_skills': ['Go'], 'preferred_skills': []}, BlindText(text=text, masked=False))
    assert result['required'][0]['mentioned'] is False


@pytest.mark.parametrize('text', [
    'Worked with Go and PostgreSQL to build APIs.',
    'Developed services using Go.',
    'Programming languages: Go, Python, SQL',
    'Built Golang services.',
    'Experienced Go developer.',
    'I go to meetings and developed services using Go.',
    'Worked with go-to-market teams and built APIs using Go.',
    'Go',
    'Go, C++',
    'Python, Go',
    'Technical skills: Go, Python',
    'Skills: Python, Go, SQL',
])
def test_explicit_go_language_evidence_is_retained(text):
    result = _score_blind({'required_skills': ['Go'], 'preferred_skills': []}, BlindText(text=text, masked=False))
    assert result['required'][0]['mentioned'] is True
    assert all(excerpt in text for excerpt in result['required'][0]['evidence'])


def test_golang_job_requirement_accepts_explicit_go():
    result = _score_blind({'required_skills': ['Golang'], 'preferred_skills': []},
                          BlindText(text='Developed services using Go.', masked=False))
    assert result['required'][0]['mentioned'] is True


def test_direct_and_graph_screen_use_the_same_go_context(tmp_path):
    path = tmp_path / 'go-context.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            db.execute("UPDATE jobs SET required_skills=?, preferred_skills='[]' WHERE id='job-backend-python'", (json.dumps(['Go']),))
            ids = [db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','fictional.pdf',?,'submitted')", (candidate, text)).lastrowid
                   for text in ('I go to meetings.', 'Built services using Go.')]
        for application_id, expected in zip(ids, (False, True), strict=True):
            direct = client.post(f'/api/applications/{application_id}/screen')
            assert direct.status_code == 200, direct.text
            assert direct.json()['required'][0]['mentioned'] is expected
            graph = client.post('/api/graph-reviews', json={'application_id': application_id, 'intent': 'full'})
            assert graph.status_code == 201, graph.text
            assert graph.json()['state']['matching']['required'][0]['mentioned'] is expected
