"""An interview guide suggests questions; it does not assess candidates."""

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.interview_guide import guide_from_matching
from app.main import create_app


@pytest.mark.parametrize('change, expected', [('role', 403), ('source', 409), ('logout', 401)])
def test_guide_rechecks_access_and_source_after_build(tmp_path, monkeypatch, change, expected):
    from app import interview_guide as guide_module

    path = tmp_path / 'guide-race.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            owner = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.pdf','Python','submitted')""", (owner,)).lastrowid
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        original = guide_module.guide_from_matching

        def concurrent_change(snapshot):
            result = original(snapshot)
            with connect(path) as db:
                if change == 'role':
                    db.execute("UPDATE users SET role='employee' WHERE username='hr'")
                elif change == 'logout':
                    db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
                else:
                    db.execute("UPDATE applications SET resume_text='Changed' WHERE id=?", (application,))
            return result

        monkeypatch.setattr(guide_module, 'guide_from_matching', concurrent_change)
        assert client.get(f'/api/applications/{application}/interview-guide').status_code == expected


def test_guide_is_job_specific_neutral_and_does_not_copy_resume():
    snapshot = {'status': 'current', 'assessment_id': 2, 'criteria': [
        {'criterion_ref': 'c-1', 'category': 'mandatory', 'criterion': 'Python', 'status': 'met',
         'evidence': [{'ref': 'e-1', 'excerpt': 'private resume fragment',
                       'cautions': [{'kind': 'negation', 'text': 'no'}]}]},
        {'criterion_ref': 'c-2', 'category': 'projects', 'criterion': 'Data pipelines', 'status': 'unknown',
         'evidence': []},
        {'criterion_ref': 'c-3', 'category': 'education', 'criterion': 'Age over 25', 'status': 'unknown',
         'evidence': []},
        {'criterion_ref': 'c-4', 'category': 'experience', 'criterion': 'email: person@example.test',
         'status': 'unknown', 'evidence': []},
    ]}
    guide = guide_from_matching(snapshot)
    assert guide['status'] == 'current' and guide['omitted_count'] == 2
    assert [q['criterion_ref'] for q in guide['questions']] == ['c-1', 'c-2']
    assert guide['questions'][0]['evidence_refs'] == ['e-1']
    assert guide['questions'][0]['caution_types'] == ['negation']
    assert 'if any' in guide['questions'][0]['question']
    assert 'personally contribute' in guide['questions'][1]['question']
    assert 'private resume fragment' not in str(guide)
    assert 'Age over 25' not in str(guide)
    assert 'incomplete' in guide['notice']


def test_unavailable_or_stale_assessments_do_not_produce_questions():
    for status in ('not_assessed', 'outdated', 'stale'):
        result = guide_from_matching({'status': status, 'criteria': [{'criterion': 'Python'}]})
        assert result['status'] == status and result['questions'] == []


def test_hr_endpoint_uses_current_assessment_and_rejects_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'guide.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.pdf','Python SQL','submitted')""", (candidate,)).lastrowid
        endpoint = f'/api/applications/{application}/interview-guide'
        assert client.get(endpoint).json()['status'] == 'not_assessed'
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria',
                                json={'category': 'mandatory', 'label': 'Python'}).json()['criterion_ref']
        evidence = client.post(f'/api/applications/{application}/rubric/evidence',
                               json={'start': 0, 'end': 6}).json()['evidence_ref']
        saved = client.post(f'/api/applications/{application}/rubric/assessment', json={'judgments': [
            {'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]},
        ]})
        assert saved.status_code == 201
        response = client.get(endpoint)
        assert response.status_code == 200 and response.json()['questions'][0]['evidence_refs'] == [evidence]
        assert 'Python SQL' not in response.text
        review = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'full'})
        assert review.status_code == 201
        assert review.json()['state']['interview_guide']['questions'][0]['criterion_ref'] == criterion
        review_id = review.json()['id']
        with connect(path) as db:
            db.execute("UPDATE applications SET resume_text='Changed source' WHERE id=?", (application,))
        assert client.get(endpoint).json()['status'] == 'outdated'
        assert client.get(f'/api/graph-reviews/{review_id}').json()['state']['interview_guide']['questions'] == []
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        assert client.get(endpoint).status_code == 403
