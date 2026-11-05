"""Distinct programming languages must not be counted as the C language."""
import json

import pytest
from fastapi.testclient import TestClient

from app.blind import BlindText
from app.db import connect
from app.main import create_app
from app.recruitment import _score_blind


@pytest.mark.parametrize('skill,text,expected', [
    ('C', 'Developed C++ services.', False),
    ('C', 'Developed C# services.', False),
    ('C', 'Used C ++ on embedded systems.', False),
    ('C', 'Developed C software.', True),
    ('C', 'Used C, C++ and C#.', True),
    ('C', 'Used C/C++ for systems.', True),
    ('C', 'Used C.\nUsed C++.', True),
    ('C++', 'Developed C++ services.', True),
    ('C#', 'Developed C# services.', True),
    ('C++', 'Developed C services.', False),
    ('C#', 'Developed C services.', False),
    ('C++', 'Developed C# services.', False),
    ('C#', 'Developed C++ services.', False),
    ('C', 'Developed Ｃ＃ services.', False),
    ('C', 'Used C!\nUsed C++.', True),
])
def test_language_mentions_are_distinct(skill, text, expected):
    result = _score_blind({'required_skills': [skill], 'preferred_skills': []}, BlindText(text=text, masked=False))
    row = result['required'][0]
    assert row['mentioned'] is expected
    assert result['score'] == (100 if expected else 0)
    assert all(quote in text for quote in row['evidence'])


def test_direct_and_graph_screen_distinguish_c_and_cpp(tmp_path):
    path = tmp_path / 'languages.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            db.execute("UPDATE jobs SET required_skills=?, preferred_skills='[]' WHERE id='job-backend-python'", (json.dumps(['C']),))
            app_ids = [db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','fictional.pdf',?,'submitted')", (candidate, text)).lastrowid for text in ('Developed C++ services.', 'Developed C++ services.')]
        direct = client.post(f'/api/applications/{app_ids[0]}/screen')
        assert direct.status_code == 200, direct.text
        assert direct.json()['required'][0]['mentioned'] is False
        graph = client.post('/api/graph-reviews', json={'application_id': app_ids[1], 'intent': 'screen_application'})
        assert graph.status_code == 201, graph.text
        preview = graph.json()['state']['screening_preview']['score']
        assert preview['required'][0]['mentioned'] is False
