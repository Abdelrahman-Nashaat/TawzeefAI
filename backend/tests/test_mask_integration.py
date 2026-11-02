"""The experimental mask affects source consumers and invalidates old reviews."""

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_mask_change_revert_keeps_old_evidence_and_publication_stale(tmp_path, monkeypatch):
    path = tmp_path / 'mask-integration.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': role + '-demo-only',
            }).status_code == 200

        login('hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','synthetic.pdf','Julia Smith\nBuilt Python and SQL services.')""",
                (candidate,)).lastrowid
        base = f'/api/applications/{application_id}'
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python',
        }).json()['criterion_ref']
        source = client.get(base + '/rubric/source').json()
        start = source['text'].index('Python')
        fields = client.get(base + '/resume/field-review').json()
        assert client.put(base + '/resume/field-review', json={
            'source_sha256': fields['source_sha256'], 'expected_source_context': fields['source_context'],
            'expected_version': fields['version'], 'fields': [
                {'record_ref': 'r-1', 'section': 'experience', 'field': 'role', 'start': start, 'end': start + 6},
            ],
        }).status_code == 200
        evidence = client.post(base + '/rubric/evidence', json={'start': start, 'end': start + 6}).json()['evidence_ref']
        assessment = client.post(base + '/rubric/assessment', json={'judgments': [
            {'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]},
        ]})
        assert assessment.status_code == 201
        preview = client.get(base + '/rubric/candidate-publication').json()
        assert client.put(base + '/rubric/candidate-publication', json={
            'assessment_id': preview['assessment_id'], 'snapshot_digest': preview['snapshot_digest'],
        }).status_code == 200
        assert client.post(base + '/screen').status_code == 200

        def save(spans):
            view = client.get(base + '/resume/mask').json()
            return client.put(base + '/resume/mask', json={
                'source_sha256': view['source_sha256'], 'expected_source_revision': view['source_revision'],
                'expected_revision': view['revision'], 'spans': spans,
            })

        assert save([{'start': 0, 'end': len('Julia Smith')}]).status_code == 200
        assert 'Julia Smith' not in client.get(base + '/rubric/source').json()['text']
        assert client.get(base + '/rubric/assessment').json()['outdated'] is True
        assert client.get(base + '/rubric/candidate-publication').json()['has_publication'] is False
        assert client.get(base).json()['score'] is None
        assert client.get(base + '/resume/mask/prior-scores').json()['entries']
        assert save([]).status_code == 200
        restored = client.get(base + '/rubric/source').json()
        assert restored['text'] == source['text'] and restored['source_context'] != source['source_context']
        assert client.get(base + '/rubric/assessment').json()['outdated'] is True
        assert client.get(base + '/resume/field-review').json()['status'] == 'stale'
        assert client.get(base + '/rubric/evidence').json() == []
        response = client.post(base + '/rubric/assessment', json={
            'expected_source_context': restored['source_context'],
            'judgments': [{'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]}],
        })
        assert response.status_code == 409
        monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
        monkeypatch.setenv('TAWZEEF_ENABLE_MODEL_RUBRIC_PROPOSALS', '1')
        from app import model_evidence_proposal
        monkeypatch.setattr(model_evidence_proposal, '_request_proposal',
                            lambda _: (_ for _ in ()).throw(AssertionError('Stale evidence reached the model')))
        context = client.get(base + '/rubric/evidence-bundle/context').json()
        result = client.post(base + '/rubric/model-evidence-proposal', json={
            'snapshot': context['snapshot'], 'confirmed': True,
            'selections': [{'criterion_ref': criterion, 'evidence_refs': [evidence]}],
        })
        assert result.status_code == 409


def test_masked_name_no_longer_counts_as_skill_in_current_previews(tmp_path):
    path = tmp_path / 'counterfactual.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': role + '-demo-only',
            }).status_code == 200

        login('hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','synthetic.pdf','Python Smith\nSkills: SQL')""", (candidate,)).lastrowid
        base = f'/api/applications/{application_id}'
        before = client.post(base + '/screen').json()
        assert next(item for item in before['required'] if item['skill'] == 'Python')['mentioned'] is True
        view = client.get(base + '/resume/mask').json()
        assert client.put(base + '/resume/mask', json={
            'source_sha256': view['source_sha256'], 'expected_source_revision': view['source_revision'],
            'expected_revision': view['revision'], 'spans': [{'start': 0, 'end': len('Python Smith')}],
        }).status_code == 200
        after = client.post(base + '/screen').json()
        assert next(item for item in after['required'] if item['skill'] == 'Python')['mentioned'] is False
        assert 'Python Smith' not in str(after)
        login('candidate')
        preview = client.get(base + '/candidate-screening').json()
        assert next(item for item in preview['required'] if item['skill'] == 'Python')['mentioned'] is False


def test_mask_change_and_revert_during_semantic_work_is_rejected(tmp_path, monkeypatch):
    from app import semantic_screening

    app = create_app(tmp_path / 'inflight.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','synthetic.pdf','Julia Smith\nBuilt Python systems.')""", (candidate,)).lastrowid
        base = f'/api/applications/{app_id}'
        assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python',
        }).status_code == 201

        def change_and_revert(_source, _criteria):
            for spans in ([{'start': 0, 'end': len('Julia Smith')}], []):
                view = client.get(base + '/resume/mask').json()
                response = client.put(base + '/resume/mask', json={
                    'source_sha256': view['source_sha256'], 'expected_source_revision': view['source_revision'],
                    'expected_revision': view['revision'], 'spans': spans,
                })
                assert response.status_code == 200
            return []

        monkeypatch.setattr(semantic_screening, 'suggest', change_and_revert)
        assert client.post(base + '/rubric/suggestions').status_code == 409
