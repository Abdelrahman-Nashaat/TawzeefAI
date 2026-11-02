"""Versioned reviewer masks are source-bound and fail closed."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.recruitment import score_resume
from app.resume_mask import initialize_resume_masks, masked_source


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'mask.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        initialize_resume_masks(path)
        initialize_resume_masks(path)
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','test.pdf',?)""",
                (candidate, 'Julia Smith\n👩‍💻 Skills: SQL')).lastrowid
        yield path, client, app_id


def payload(view, spans):
    return {'source_sha256': view['source_sha256'], 'expected_source_revision': view['source_revision'],
            'expected_revision': view['revision'], 'spans': spans}


def test_review_endpoint_requires_authentication(tmp_path):
    app = create_app(tmp_path / 'authentication.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.get('/api/applications/1/resume/mask').status_code == 401


def test_masked_source_preserves_offsets_and_changes_name_based_mention(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/mask'
    view = client.get(endpoint).json()
    assert view['status'] == 'unreviewed' and view['offset_unit'] == 'unicode_code_points'
    with connect(path) as db:
        before = masked_source(db, app_id)
    assert before['context_sha256'] == before['source_sha256']
    assert score_resume({'required_skills': ['Julia'], 'preferred_skills': []}, before['text'])['score'] == 100
    saved = client.put(endpoint, json=payload(view, [{'start': 0, 'end': len('Julia Smith')}]))
    assert saved.status_code == 200 and saved.json()['status'] == 'reviewed'
    with connect(path) as db:
        result = masked_source(db, app_id)
        stored = db.execute('SELECT spans_json FROM resume_masks').fetchone()[0]
        audit = db.execute("SELECT details FROM audit_events WHERE action='resume_mask_reviewed'").fetchone()[0]
    assert len(result['text']) == len(view['source'])
    assert result['text'].index('👩') == view['source'].index('👩')
    assert 'Julia Smith' not in result['text'] and 'Julia' not in stored + audit
    assert score_resume({'required_skills': ['Julia'], 'preferred_skills': []}, result['text'])['score'] == 0
    assert result['context_sha256'] != before['context_sha256']
    assert client.get(endpoint).json()['spans'] == [{'start': 0, 'end': len('Julia Smith')}]


def test_source_and_mask_change_then_revert_cannot_revive_old_context(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/mask'
    view = client.get(endpoint).json()
    selection = [{'start': 0, 'end': len('Julia Smith')}]
    assert client.put(endpoint, json=payload(view, selection)).status_code == 200
    with connect(path) as db:
        original_context = masked_source(db, app_id)['context_sha256']
    view = client.get(endpoint).json()
    assert client.put(endpoint, json=payload(view, [])).status_code == 200
    view = client.get(endpoint).json()
    assert client.put(endpoint, json=payload(view, selection)).status_code == 200
    with connect(path) as db:
        assert masked_source(db, app_id)['context_sha256'] != original_context
        db.execute("UPDATE applications SET resume_text='Alex Smith\\nSkills: SQL' WHERE id=?", (app_id,))
        db.execute("UPDATE applications SET resume_text='Julia Smith\\n👩‍💻 Skills: SQL' WHERE id=?", (app_id,))
        with pytest.raises(HTTPException) as caught:
            masked_source(db, app_id)
        assert caught.value.status_code == 409
    stale = client.get(endpoint).json()
    assert stale['status'] == 'stale' and stale['spans'] == [] and stale['source_revision'] == 2
    assert client.put(endpoint, json=payload(view, selection)).status_code == 409
    assert client.put(endpoint, json=payload(stale, selection)).status_code == 200
    with connect(path) as db:
        assert masked_source(db, app_id)['context_sha256'] != original_context


def test_conflicting_writers_and_invalid_masks_fail_closed(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/mask'
    view = client.get(endpoint).json()
    for spans in ([{'start': 0, 'end': 2}, {'start': 1, 'end': 4}],
                  [{'start': 0, 'end': len(view['source']) + 1}],
                  [{'start': 0, 'end': True}]):
        assert client.put(endpoint, json=payload(view, spans)).status_code == 422
    item = payload(view, [{'start': 0, 'end': 5}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.put(endpoint, json=item).status_code, range(2)))
    assert sorted(results) == [200, 409]
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM resume_masks').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='resume_mask_reviewed'").fetchone()[0] == 1
        db.execute("UPDATE resume_masks SET spans_json='[{}]' WHERE application_id=?", (app_id,))
        with pytest.raises(HTTPException) as caught:
            masked_source(db, app_id)
        assert caught.value.status_code == 409
    assert client.get(endpoint).json()['status'] == 'stale'
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(endpoint).status_code == 403
    assert client.put(endpoint, json=item).status_code == 403


def test_inconsistent_revision_state_does_not_fall_back_to_unreviewed(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/mask'
    view = client.get(endpoint).json()
    assert client.put(endpoint, json=payload(view, [{'start': 0, 'end': 5}])).status_code == 200
    with connect(path) as db:
        db.execute('UPDATE resume_mask_state SET mask_revision=0 WHERE application_id=?', (app_id,))
        with pytest.raises(HTTPException) as caught:
            masked_source(db, app_id)
        assert caught.value.status_code == 409
    assert client.get(endpoint).status_code == 409


def test_audit_failure_rolls_back_mask_and_revision(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/mask'
    item = payload(client.get(endpoint).json(), [{'start': 0, 'end': 5}])
    with connect(path) as db:
        db.execute("UPDATE applications SET score_json=? WHERE id=?", ('{\"score\":42}', app_id))
        db.execute("""CREATE TRIGGER abort_mask BEFORE INSERT ON audit_events
            WHEN NEW.action='resume_mask_reviewed' BEGIN SELECT RAISE(ABORT, 'failure'); END""")
    assert client.put(endpoint, json=item).status_code == 500
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM resume_masks').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM resume_mask_state').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM resume_mask_prior_scores').fetchone()[0] == 0
        assert db.execute('SELECT score_json FROM applications WHERE id=?', (app_id,)).fetchone()[0] == '{"score":42}'


def test_mask_review_archives_score_and_invalidates_publication_atomically(setup):
    path, client, app_id = setup
    endpoint = f'/api/applications/{app_id}/resume/mask'
    view = client.get(endpoint).json()
    with connect(path) as db:
        db.execute("UPDATE applications SET score_json=? WHERE id=?", ('{\"score\":42}', app_id))
        before = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?', (app_id,)).fetchone()
        previous_revision = before[0] if before else 0
    assert client.put(endpoint, json=payload(view, [{'start': 0, 'end': 5}])).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT score_json FROM applications WHERE id=?', (app_id,)).fetchone()[0] is None
        assert db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?', (app_id,)).fetchone()[0] == previous_revision + 1
    archived = client.get(endpoint + '/prior-scores')
    assert archived.status_code == 200 and archived.json()['entries'][0]['result'] == {'score': 42}
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(endpoint + '/prior-scores').status_code == 403
