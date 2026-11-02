"""Pending reviews created with the previous matching digest must remain safe."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.blind import redact_resume
from app.db import connect
from app.main import create_app
from app.recruitment import job_data, score_masked_resume, score_resume
from app.resume_mask import masked_source


def _legacy_matching_digest(db, application_id):
    """Freeze the matching snapshot serialization from before masking integration."""
    application = db.execute('SELECT * FROM applications WHERE id=?', (application_id,)).fetchone()
    job = db.execute('SELECT * FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
    criteria = [dict(row) for row in db.execute(
        'SELECT id,category,label FROM rubric_criteria WHERE job_id=? ORDER BY id', (application['job_id'],))]
    assessment = db.execute('''SELECT id,assessment_json,result_json,created_at,criteria_fingerprint,source_fingerprint
        FROM rubric_assessments WHERE application_id=? ORDER BY id DESC LIMIT 1''', (application_id,)).fetchone()
    evidence = [dict(row) for row in db.execute('''SELECT id,start_offset,end_offset,excerpt
        FROM rubric_evidence WHERE application_id=? ORDER BY id''', (application_id,))]
    review = db.execute('''SELECT version,source_sha256,fields_json,created_by,created_at
        FROM resume_field_reviews WHERE application_id=? ORDER BY version DESC LIMIT 1''', (application_id,)).fetchone()
    raw = json.dumps({'application_id': application_id, 'job': dict(job),
        'source': redact_resume(application['resume_text']).text, 'criteria': criteria,
        'assessment': dict(assessment) if assessment else None, 'evidence': evidence,
        'field_review': dict(review) if review else None}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def test_unreviewed_score_keeps_previous_checkpoint_shape(tmp_path):
    app = create_app(tmp_path / 'screen-score.sqlite3', seed_demo=True)
    with TestClient(app), connect(app.state.database_path) as db:
        candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
        application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
            VALUES (?,'job-backend-python','synthetic.pdf','Python SQL')""", (candidate,)).lastrowid
        job = job_data(db.execute("SELECT * FROM jobs WHERE id='job-backend-python'").fetchone())
        assert score_masked_resume(job, masked_source(db, application)) == score_resume(job, 'Python SQL')


@pytest.mark.parametrize('change', ['none', 'mask', 'source'])
@pytest.mark.parametrize('has_field_review', [False, True])
def test_legacy_advance_checkpoint_checks_mask_revisions(tmp_path, monkeypatch, change, has_field_review):
    from app import graph_orchestrator

    path = tmp_path / 'migration.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.pdf','Python SQL','submitted')""", (candidate,)).lastrowid
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria',
                                json={'category': 'mandatory', 'label': 'Python'}).json()['criterion_ref']
        evidence = client.post(f'/api/applications/{application}/rubric/evidence',
                               json={'start': 0, 'end': 6}).json()['evidence_ref']
        response = client.post(f'/api/applications/{application}/rubric/assessment', json={'judgments': [
            {'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]},
        ]})
        assert response.status_code == 201
        if has_field_review:
            fields = client.get(f'/api/applications/{application}/resume/field-review').json()
            assert client.put(f'/api/applications/{application}/resume/field-review', json={
                'source_sha256': fields['source_sha256'], 'expected_version': 0, 'fields': [],
            }).status_code == 200
        with connect(path) as db:
            # Rows migrated from the old schema have no source_context value.
            db.execute('UPDATE rubric_evidence SET source_context=NULL WHERE application_id=?', (application,))
            db.execute('UPDATE rubric_assessments SET source_context=NULL WHERE application_id=?', (application,))
            db.execute('UPDATE resume_field_reviews SET source_context=NULL WHERE application_id=?', (application,))
            legacy_digest = _legacy_matching_digest(db, application)
        current = graph_orchestrator.matching_snapshot

        def old_snapshot(db, app_id):
            _, report = current(db, app_id)
            return _legacy_matching_digest(db, app_id), report

        monkeypatch.setattr(graph_orchestrator, 'matching_snapshot', old_snapshot)
        result = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'advance_application'})
        assert result.status_code == 201, result.text
        review = result.json()
        assert review['awaiting_review']
        assert review['state']['matching_specialist']['status'] == 'current'
        with connect(path) as db:
            assert _legacy_matching_digest(db, application) == legacy_digest
        monkeypatch.setattr(graph_orchestrator, 'matching_snapshot', current)
        if change == 'mask':
            endpoint = f'/api/applications/{application}/resume/mask'
            for spans in ([{'start': 0, 'end': 6}], []):
                view = client.get(endpoint).json()
                response = client.put(endpoint, json={
                    'source_sha256': view['source_sha256'], 'expected_source_revision': view['source_revision'],
                    'expected_revision': view['revision'], 'spans': spans,
                })
                assert response.status_code == 200
        elif change == 'source':
            with connect(path) as db:
                db.execute("UPDATE applications SET resume_text='Different' WHERE id=?", (application,))
                db.execute("UPDATE applications SET resume_text='Python SQL' WHERE id=?", (application,))
        result = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
            'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint'],
        })
        if change != 'none':
            assert result.status_code == 409
        else:
            assert result.status_code == 200, result.text
            assert result.json()['state']['action_result']['status'] == 'advanced'
