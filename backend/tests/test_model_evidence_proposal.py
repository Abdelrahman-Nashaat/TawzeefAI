"""Explicit, HR-only source-bound model proposals never create decisions."""
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('TAWZEEF_ENABLE_MODEL_RUBRIC_PROPOSALS', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'dummy-key')
    app = create_app(tmp_path / 'proposal.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','No Python experience','submitted')", (candidate,)).lastrowid
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'mandatory', 'label': 'Python'}).json()['criterion_ref']
        evidence = client.post(f'/api/applications/{app_id}/rubric/evidence', json={'start': 0, 'end': 20}).json()['evidence_ref']
        context = client.get(f'/api/applications/{app_id}/rubric/evidence-bundle/context').json()
        payload = {'snapshot': context['snapshot'], 'confirmed': True,
                   'selections': [{'criterion_ref': criterion, 'evidence_refs': [evidence]}]}
        yield client, app.state.database_path, app_id, payload


def selection(payload, relation='unclear', reason='ambiguous'):
    return json.dumps({'proposals': [{'criterion_ref': payload['selections'][0]['criterion_ref'],
        'evidence_ref': payload['selections'][0]['evidence_refs'][0], 'relation': relation, 'reason_code': reason}]})


def test_explicit_preview_has_no_writes_and_sends_only_selected_material(case, monkeypatch):
    from app import model_evidence_proposal as module
    client, path, app_id, payload = case
    called = []
    def fake(items):
        called.append(items)
        return selection(payload, 'contradicting', 'negated')
    monkeypatch.setattr(module, '_request_proposal', fake)
    with connect(path) as db:
        before = db.total_changes
        counts = [db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('rubric_assessments', 'audit_events', 'rubric_audit_events')]
    response = client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['proposals'][0]['relation'] == 'contradicting'
    proposal = response.json()['proposals'][0]
    assert proposal['cue_offset_unit'] == 'excerpt_unicode_code_points'
    assert proposal['text_cues'] == [{'kind': 'negation', 'text': 'No', 'start': 0, 'end': 2}]
    assert proposal['model_label_conflict'] is False
    assert called == [[{'criterion_ref': payload['selections'][0]['criterion_ref'], 'criterion': 'Python',
                        'evidence_ref': payload['selections'][0]['evidence_refs'][0], 'excerpt': 'No Python experience'}]]
    with connect(path) as db:
        assert [db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('rubric_assessments', 'audit_events', 'rubric_audit_events')] == counts
    assert before == 0


def test_inconsistent_model_label_is_flagged_without_rewriting_it(case, monkeypatch):
    from app import model_evidence_proposal as module

    client, _, app_id, payload = case
    monkeypatch.setattr(module, '_request_proposal', lambda _: selection(payload, 'supporting', 'negated'))
    response = client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload)
    assert response.status_code == 200
    proposal = response.json()['proposals'][0]
    assert proposal['relation'] == 'supporting'
    assert proposal['model_label_conflict'] is True
    assert proposal['text_cues'][0]['text'] == 'No'
    assert 'do not determine their scope' in response.json()['notice']


def test_cue_in_unrelated_clause_does_not_override_model_proposal(case, monkeypatch):
    from app import model_evidence_proposal as module

    client, path, app_id, payload = case
    source = 'I used Python, but never used Java.'
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', (source, app_id))
    current = client.get(f'/api/applications/{app_id}/rubric/source').json()
    evidence = client.post(f'/api/applications/{app_id}/rubric/evidence', json={
        'start': 0, 'end': len(source), 'expected_source_sha256': current['source_sha256'],
        'expected_source_context': current['source_context'],
    })
    assert evidence.status_code == 201
    payload['selections'][0]['evidence_refs'] = [evidence.json()['evidence_ref']]
    payload['snapshot'] = client.get(f'/api/applications/{app_id}/rubric/evidence-bundle/context').json()['snapshot']
    monkeypatch.setattr(module, '_request_proposal', lambda _: selection(payload, 'supporting', 'explicit_claim'))
    response = client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload)
    assert response.status_code == 200
    proposal = response.json()['proposals'][0]
    assert proposal['relation'] == 'supporting' and proposal['model_label_conflict'] is False
    cue = proposal['text_cues'][0]
    assert source[cue['start']:cue['end']] == 'never'


@pytest.mark.parametrize('bad', ['foreign', 'duplicate', 'extra', 'invalid_relation', 'missing'])
def test_invalid_model_output_is_rejected(case, monkeypatch, bad):
    from app import model_evidence_proposal as module
    client, _, app_id, payload = case
    parsed = json.loads(selection(payload))
    if bad == 'foreign': parsed['proposals'][0]['evidence_ref'] = 'e-9999'
    elif bad == 'duplicate': parsed['proposals'].append(parsed['proposals'][0])
    elif bad == 'extra': parsed['proposals'][0]['explanation'] = 'injected text'
    elif bad == 'invalid_relation': parsed['proposals'][0]['relation'] = 'hire'
    else: parsed['proposals'] = []
    monkeypatch.setattr(module, '_request_proposal', lambda _: json.dumps(parsed))
    response = client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload)
    assert response.status_code == 502
    assert 'injected text' not in response.text


def test_stale_after_call_is_not_returned(case, monkeypatch):
    from app import model_evidence_proposal as module
    client, path, app_id, payload = case
    def mutate(_):
        with connect(path) as db:
            db.execute("UPDATE applications SET resume_text='Changed' WHERE id=?", (app_id,))
        return selection(payload)
    monkeypatch.setattr(module, '_request_proposal', mutate)
    assert client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload).status_code == 409


def test_session_expiry_after_call_hides_result(case, monkeypatch):
    from app import model_evidence_proposal as module
    client, _, app_id, payload = case
    monkeypatch.setattr(module, '_request_proposal', lambda _: selection(payload))
    original = module._require_current_role
    calls = 0
    def authorize(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)
    monkeypatch.setattr(module, '_require_current_role', authorize)
    response = client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload)
    assert response.status_code == 401 and 'proposals' not in response.text


def test_unknown_evidence_never_reaches_provider(case, monkeypatch):
    from app import model_evidence_proposal as module
    client, _, app_id, payload = case
    payload['selections'][0]['evidence_refs'] = ['e-9999']
    monkeypatch.setattr(module, '_request_proposal', lambda _: pytest.fail('Invalid evidence sent to provider'))
    assert client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload).status_code == 409


@pytest.mark.parametrize('change', ['criterion', 'evidence_reverted', 'owner_reverted'])
def test_changed_selection_cannot_reach_provider_even_if_restored(case, monkeypatch, change):
    from app import model_evidence_proposal as module

    client, path, app_id, payload = case
    with connect(path) as db:
        if change == 'criterion':
            db.execute("UPDATE rubric_criteria SET label='Changed' WHERE id=?", (int(payload['selections'][0]['criterion_ref'][2:]),))
        elif change == 'evidence_reverted':
            evidence_id = int(payload['selections'][0]['evidence_refs'][0][2:])
            db.execute("UPDATE rubric_evidence SET excerpt='changed' WHERE id=?", (evidence_id,))
            db.execute("UPDATE rubric_evidence SET excerpt='No Python experience' WHERE id=?", (evidence_id,))
        else:
            owner = db.execute('SELECT candidate_id FROM applications WHERE id=?', (app_id,)).fetchone()[0]
            other = db.execute("SELECT id FROM users WHERE username='employee'").fetchone()[0]
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, app_id))
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (owner, app_id))
    monkeypatch.setattr(module, '_request_proposal', lambda _: pytest.fail('Stale selection sent to provider'))
    assert client.post(f'/api/applications/{app_id}/rubric/model-evidence-proposal', json=payload).status_code == 409


def test_explicit_enable_confirmation_and_hr_role(case, monkeypatch):
    client, _, app_id, payload = case
    endpoint = f'/api/applications/{app_id}/rubric/model-evidence-proposal'
    assert client.post(endpoint, json={**payload, 'confirmed': False}).status_code == 422
    monkeypatch.setenv('TAWZEEF_ENABLE_MODEL_RUBRIC_PROPOSALS', '0')
    assert client.post(endpoint, json=payload).status_code == 503
    monkeypatch.setenv('TAWZEEF_ENABLE_MODEL_RUBRIC_PROPOSALS', '1')
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.post(endpoint, json=payload).status_code == 403
