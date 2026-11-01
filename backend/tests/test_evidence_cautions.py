"""Cautions identify exact source text and never change human assessments."""

from fastapi.testclient import TestClient

from app.db import connect
from app.evidence_cautions import MAX_CAUTIONS, evidence_cautions
from app.main import create_app


def test_cues_have_exact_offsets_and_do_not_treat_not_only_as_negation():
    source = 'Intro\nI have no Python experience and plan to learn it. My colleague uses SQL.'
    excerpt = source[source.index('I have'):]
    flags = evidence_cautions(excerpt, source.index('I have'))
    assert [flag['kind'] for flag in flags] == ['negation', 'aspiration', 'other_person']
    assert all(source[flag['start']:flag['end']] == flag['text'] for flag in flags)
    assert evidence_cautions('Not only Python but also SQL.', 0) == []
    assert len(evidence_cautions('no ' * 50, 0)) == MAX_CAUTIONS
    text = '\u0644\u0627 \u062e\u0628\u0631\u0629 Python. \u0623\u0631\u063a\u0628 \u0641\u064a \u062a\u0639\u0644\u0645 SQL.'
    arabic_flags = evidence_cautions(text, 0)
    assert [flag['kind'] for flag in arabic_flags] == ['negation', 'aspiration']
    assert all(text[flag['start']:flag['end']] == flag['text'] for flag in arabic_flags)


def test_reviewed_evidence_cues_do_not_change_score_or_reveal_hr_assessment(tmp_path):
    path = tmp_path / 'review.sqlite3'
    source = 'Skills\nI have no Python experience.'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','synthetic.docx',?)''', (candidate, source)).lastrowid
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python'}).json()['criterion_ref']
        excerpt = 'I have no Python experience.'
        start = source.index(excerpt)
        evidence = client.post(f'/api/applications/{app_id}/rubric/evidence', json={
            'start': start, 'end': start + len(excerpt)}).json()['evidence_ref']
        assessment = client.post(f'/api/applications/{app_id}/rubric/assessment', json={
            'judgments': [{'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]}]})
        assert assessment.status_code == 201
        review = client.post('/api/graph-reviews', json={'application_id': app_id})
        assert review.status_code == 201
        matching = review.json()['state']['matching_specialist']
        item = matching['criteria'][0]
        assert item['status'] == 'met' and matching['bounds']['minimum'] == 40
        flag = item['evidence'][0]['cautions'][0]
        assert flag['kind'] == 'negation' and source[flag['start']:flag['end']] == flag['text'] == 'no'
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        assert client.get(f'/api/graph-reviews/{review.json()["id"]}').status_code == 403
        assert client.get(f'/api/applications/{app_id}/candidate-assessment').json() == {'status': 'unavailable'}
