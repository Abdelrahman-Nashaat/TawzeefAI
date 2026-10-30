from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')
    path = tmp_path / 'screening.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        def login(role):
            response = client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'})
            assert response.status_code == 200

        login('candidate')
        document = Document()
        for line in ['Name: Sample', 'Skills', '👩‍💻 Python و تحليل البيانات', 'Experience',
                     'I never used Python in a professional role.', 'Projects', 'Built a Java app']:
            document.add_paragraph(line)
        data = BytesIO()
        document.save(data)
        response = client.post('/api/applications', params={
            'job_id': 'job-backend-python', 'filename': 'resume.docx',
        }, content=data.getvalue())
        assert response.status_code == 201
        application_id = response.json()['id']
        login('hr')
        for label in ['Python', 'Java', 'Rust']:
            assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
                'category': 'mandatory', 'label': label,
            }).status_code == 201
        yield client, path, application_id, login


class FakeLocalModel:
    def __init__(self, callback=None):
        self.callback = callback

    def encode(self, texts, *, query):
        if query and self.callback:
            self.callback()
            self.callback = None
        vectors = []
        for text in texts:
            if 'python' in text.casefold():
                vectors.append([1., 0., 0.])
            elif 'java' in text.casefold():
                vectors.append([0., 1., 0.])
            else:
                vectors.append([0., 0., 1.])
        return vectors


def test_hr_suggestions_offsets_and_manual_evidence(setup, monkeypatch):
    from app import semantic_screening

    client, path, application_id, login = setup
    monkeypatch.setattr(semantic_screening, '_model', lambda directory: FakeLocalModel())
    target = f'/api/applications/{application_id}/rubric'
    source = client.get(target + '/source').json()['text']
    response = client.post(target + '/suggestions')
    assert response.status_code == 200
    result = response.json()
    assert result['offset_unit'] == 'unicode_code_points' and result['requires_human_review']
    assert 'does not establish' in result['notice']
    python = next(item for item in result['criteria'] if item['label'] == 'Python')
    assert len(python['candidates']) == 2
    assert any('never used' in item['excerpt'] for item in python['candidates'])
    for item in python['candidates']:
        assert source[item['start']:item['end']] == item['excerpt']
        assert item['end'] - item['start'] <= 500
    assert next(item for item in result['criteria'] if item['label'] == 'Rust')['candidates'] == []
    selected = python['candidates'][0]
    expected = {key: python[key] for key in ('criterion_ref', 'category', 'label')}
    saved = client.post(target + '/evidence', json={
        'start': selected['start'], 'end': selected['end'], 'expected_source_sha256': result['source_sha256'],
        'expected_criterion': expected,
    })
    assert saved.status_code == 201 and saved.json()['excerpt'] == selected['excerpt']
    # Suggestions never create a qualification judgment or change the application.
    assert client.get(target + '/assessment').status_code == 404
    with connect(path) as db:
        db.execute('UPDATE rubric_criteria SET label=? WHERE id=?', ('Changed', int(expected['criterion_ref'][2:])))
    assert client.post(target + '/evidence', json={
        'start': selected['start'], 'end': selected['end'], 'expected_source_sha256': result['source_sha256'],
        'expected_criterion': expected,
    }).status_code == 409
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text = ? WHERE id = ?', ('Skills\nNew source', application_id))
    assert client.post(target + '/evidence', json={
        'start': selected['start'], 'end': selected['end'], 'expected_source_sha256': result['source_sha256'],
    }).status_code == 409
    login('candidate')
    assert client.post(target + '/suggestions').status_code == 403
    login('employee')
    assert client.post(target + '/suggestions').status_code == 403


@pytest.mark.parametrize('change', ['source', 'criteria', 'session', 'role'])
def test_concurrent_changes_withhold_suggestions(setup, monkeypatch, change):
    from app import semantic_screening

    client, path, application_id, _ = setup

    def mutate():
        with connect(path) as db:
            if change == 'source':
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Skills\nChanged', application_id))
            elif change == 'criteria':
                db.execute("UPDATE rubric_criteria SET label='Changed' WHERE id=(SELECT MIN(id) FROM rubric_criteria)")
            elif change == 'session':
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
            else:
                db.execute("UPDATE users SET role='employee' WHERE username='hr'")

    monkeypatch.setattr(semantic_screening, '_model', lambda directory: FakeLocalModel(mutate))
    response = client.post(f'/api/applications/{application_id}/rubric/suggestions')
    assert response.status_code == (401 if change in ('session', 'role') else 409)
    assert 'never used' not in response.text


def test_unconfigured_or_failed_model_is_explicit(setup, monkeypatch):
    from app import semantic_screening

    client, _, application_id, _ = setup
    target = f'/api/applications/{application_id}/rubric/suggestions'
    monkeypatch.delenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING')
    assert client.post(target).status_code == 503
    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    def fail(directory):
        raise ValueError('secret local path')
    monkeypatch.setattr(semantic_screening, '_model', fail)
    response = client.post(target)
    assert response.status_code == 503 and 'secret local path' not in response.text


def test_long_sentence_preserves_negation_and_unicode_offsets():
    from app.semantic_screening import _passages

    # Place negation directly across the nominal split point.
    source = 'Skills\n' + ('word ' * 97) + 'I have never worked with Python in any job. ' + ('more ' * 80)
    negated = 'I have never worked with Python in any job.'
    passages = _passages(source)
    assert any(negated in item['excerpt'] for item in passages)
    assert all(item['excerpt'] == source[item['start']:item['end']] for item in passages)
    assert all(item['end'] - item['start'] <= 500 for item in passages)


def test_unrecognized_sections_are_explicit(setup, monkeypatch):
    from app import semantic_screening

    client, path, application_id, _ = setup
    with connect(path) as db:
        db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('I have Python experience.', application_id))
    monkeypatch.setattr(semantic_screening, '_model', lambda directory: FakeLocalModel())
    response = client.post(f'/api/applications/{application_id}/rubric/suggestions')
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == 'no_recognized_sections'
    assert 'No recognizable resume sections' in result['notice']
    assert all(not item['candidates'] for item in result['criteria'])
