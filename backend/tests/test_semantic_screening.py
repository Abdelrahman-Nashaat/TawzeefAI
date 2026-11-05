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
    python = next(item for item in result['criteria'] if item['label'] == 'Python')
    assert python['candidates'][0]['section'] == 'unclassified'
    assert python['candidates'][0]['excerpt'] == 'I have Python experience.'
    assert all(not item['candidates'] for item in result['criteria'] if item['label'] != 'Python')


def test_mixed_classification_and_exact_offsets():
    from app.semantic_screening import _passages

    source = 'Skills\nPython\nProfile\n👩‍💻 Built Java systems.\nEducation: College\nUnknown: Python projects'
    passages = _passages(source)
    assert any(p['section'] == 'skills' and p['excerpt'] == 'Python' for p in passages)
    assert any(p['section'] == 'unclassified' and 'Built Java systems' in p['excerpt'] for p in passages)
    assert any(p['section'] == 'education' and p['excerpt'] == 'College' for p in passages)
    assert any(p['section'] == 'unclassified' and 'Unknown: Python projects' in p['excerpt'] for p in passages)
    assert all(source[p['start']:p['end']] == p['excerpt'] for p in passages)


def test_unclassified_passages_enforce_the_existing_limit():
    from fastapi import HTTPException

    from app.semantic_screening import _passages

    with pytest.raises(HTTPException) as exc:
        _passages('\n'.join(f'Project number {i}' for i in range(101)))
    assert exc.value.status_code == 422


def test_long_unclassified_line_is_bounded_without_duplicate_spans():
    from app.semantic_screening import _passages

    source = ' '.join(f'word{i}' for i in range(220))
    passages = _passages(source)
    spans = [(p['start'], p['end']) for p in passages]
    assert 1 < len(spans) <= 100
    assert len(spans) == len(set(spans))
    assert all(0 <= start < end <= len(source) and end - start <= 500 for start, end in spans)
    assert all(passages[i]['start'] < passages[i + 1]['start'] <= passages[i]['end']
               for i in range(len(passages) - 1))
    assert all(p['section'] == 'unclassified' and source[p['start']:p['end']] == p['excerpt'] for p in passages)


def test_semantic_suggestions_keep_adjacent_negation_context(monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')
    monkeypatch.setattr(semantic_screening, '_model', lambda directory: FakeLocalModel())
    source = 'Experience\nI have never worked with\nPython\nProjects\nBuilt a Java app'
    result = semantic_screening.suggest(source, [{'label': 'Python'}])[0]['candidates']
    assert result
    assert 'I have never worked with\nPython' in result[0]['excerpt']
    assert all(source[item['start']:item['end']] == item['excerpt'] for item in result)
    assert source[result[0]['matched_start']:result[0]['matched_end']] == 'Python'
    assert result[0]['start'] <= result[0]['matched_start'] < result[0]['matched_end'] <= result[0]['end']
    assert all('Projects' not in item['excerpt'] for item in result)


def test_semantic_suggestions_diversify_overlapping_windows(monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')
    monkeypatch.setattr(semantic_screening, '_model', lambda directory: FakeLocalModel())
    source = 'Skills\n' + 'Python ' * 150 + '\nExperience\nPython project with a separate team'
    result = semantic_screening.suggest(source, [{'label': 'Python'}])[0]['candidates']
    assert any('separate team' in item['excerpt'] for item in result)
    assert all(source[item['start']:item['end']] == item['excerpt'] for item in result)


def test_exact_skill_phrases_supplement_low_similarity_without_substring_matches(monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')

    class LowSimilarityModel:
        def encode(self, texts, *, query):
            return [[1.0, 0.0] if query else [0.0, 1.0] for _ in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: LowSimilarityModel())
    source = 'Skills\nJavaScript, C+++, C#x and Pythons\nExperience\nNo experience with C++.\n' \
             'Built React.js apps and used C#\nNever used Python'
    criteria = [{'label': label} for label in ('Java', 'C++', 'C#', 'React.js', 'Python')]
    results = semantic_screening.suggest(source, criteria)
    assert not results[0]['candidates']
    for result in results[1:]:
        assert len(result['candidates']) == 1
        item = result['candidates'][0]
        assert item['retrieval_method'] == 'exact_phrase' and item['similarity'] is None
        assert source[item['matched_start']:item['matched_end']].casefold() == result['label'].casefold()
        assert source[item['start']:item['end']] == item['excerpt']
    assert 'No experience with C++' in results[1]['candidates'][0]['excerpt']
    assert 'Never used Python' in results[4]['candidates'][0]['excerpt']


def test_exact_phrase_at_chunk_boundary_does_not_match_inside_identifier(monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')

    class LowSimilarityModel:
        def encode(self, texts, *, query):
            return [[1.0, 0.0] if query else [0.0, 1.0] for _ in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: LowSimilarityModel())
    source = 'x' * 493 + '.' + 'Python' + 'x' * 500
    assert not semantic_screening.suggest(source, [{'label': 'Python'}])[0]['candidates']


def test_exact_mentions_do_not_hide_distinct_semantic_passage(monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')

    class MeaningModel:
        def encode(self, texts, *, query):
            return [[1., 0.] if query or 'data manipulation' in text else [0., 1.] for text in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: MeaningModel())
    source = ('Skills\nPandas for cleaning\n\nExperience\nPandas reporting\n\n'
              'Projects\nPandas dashboards\n\nOther\nData manipulation with tabular data')
    # Case-insensitive model example; selection is an unverified lead for review.
    source = source.replace('Data manipulation', 'data manipulation')
    candidates = semantic_screening.suggest(source, [{'label': 'Pandas'}])[0]['candidates']
    assert len(candidates) == 3
    assert [item['retrieval_method'] for item in candidates].count('exact_phrase') == 2
    semantic = next(item for item in candidates if item['retrieval_method'] == 'semantic')
    assert 'data manipulation with tabular data' in semantic['excerpt']
    assert all(source[item['start']:item['end']] == item['excerpt'] for item in candidates)


def test_three_exact_mentions_remain_when_no_distinct_semantic_passage(monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')

    class LowSimilarityModel:
        def encode(self, texts, *, query):
            return [[1., 0.] if query else [0., 1.] for _ in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: LowSimilarityModel())
    source = 'Skills\nPandas cleaning\n\nExperience\nPandas reporting\n\nProjects\nPandas dashboards'
    candidates = semantic_screening.suggest(source, [{'label': 'Pandas'}])[0]['candidates']
    assert len(candidates) == 3
    assert all(item['retrieval_method'] == 'exact_phrase' for item in candidates)


def test_exact_passage_is_not_relabelled_semantic_to_fill_diversity(monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')
    monkeypatch.setattr(semantic_screening, '_model', lambda _: FakeLocalModel())
    source = 'Skills\nPython cleaning\n\nExperience\nPython reporting\n\nProjects\nPython dashboards'
    candidates = semantic_screening.suggest(source, [{'label': 'Python'}])[0]['candidates']
    assert len(candidates) == 3
    assert all(item['retrieval_method'] == 'exact_phrase' for item in candidates)
