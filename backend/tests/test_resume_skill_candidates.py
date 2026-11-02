from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app import recruitment
from app.db import connect
from app.main import create_app
from app.resume_structure import extract_skill_candidates, structure_resume


def candidates(source):
    return extract_skill_candidates(source, structure_resume(source))


def test_unverified_skills_have_exact_unicode_offsets_and_bullet_removal():
    source = 'Skills: Python, C++, SQL؛ تحليل البيانات\r\n- JavaScript؛ React\r\nEducation: Degree\r\n'
    result = candidates(source)
    assert [item['text'] for item in result['items']] == ['Python', 'C++', 'SQL', 'تحليل البيانات', 'JavaScript', 'React']
    for item in result['items']:
        assert source[item['start']:item['end']] == item['text']
        assert item['section_start'] <= item['start'] < item['end'] <= item['section_end']
    assert result['skipped_lines_or_fragments'] == 0
    assert not result['truncated']


def test_ambiguous_and_oversized_lines_are_excluded_and_bounded():
    text = 'Skills\nPython, SQL\nAWS (S3, EC2)\n[identity field removed]\n' + ('x' * 81) + '\n'
    result = candidates(text)
    assert [item['text'] for item in result['items']] == ['Python', 'SQL']
    assert result['skipped_lines_or_fragments'] == 2
    source = 'Skills\n' + ','.join('Skill' + str(i) for i in range(101))
    limited = candidates(source)
    assert len(limited['items']) == 100 and limited['truncated']
    assert candidates('Python and SQL without a heading')['items'] == []


def test_endpoint_requires_current_hr_and_protects_changed_mask_and_source(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'skills.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username':'candidate','password':'candidate-demo-only'}).status_code == 200
        document = Document()
        for line in ('Name: Synthetic Person', 'Skills: Python, SQL'):
            document.add_paragraph(line)
        buf = BytesIO()
        document.save(buf)
        response = client.post('/api/applications', params={'job_id':'job-backend-python','filename':'cv.docx'}, content=buf.getvalue())
        assert response.status_code == 201
        application_id = response.json()['id']
        endpoint = f'/api/applications/{application_id}/resume/structured'
        assert client.get(endpoint).status_code == 403
        assert client.post('/api/auth/login', json={'username':'hr','password':'hr-demo-only'}).status_code == 200
        response = client.get(endpoint)
        assert response.status_code == 200
        result = response.json()
        assert [item['text'] for item in result['skill_candidates']['items']] == ['Python', 'SQL']
        assert result['source_revision'] == 0 and result['mask_revision'] == 0
        assert result['mask_review_status'] == 'unreviewed'
        source = client.get(f'/api/applications/{application_id}/rubric/source').json()['text']
        assert all(source[item['start']:item['end']] == item['text'] for item in result['skill_candidates']['items'])
        assert 'Synthetic Person' not in str(result)
        mask_path = f'/api/applications/{application_id}/resume/mask'
        mask = client.get(mask_path).json()
        start = mask['source'].index('Python')
        saved = client.put(mask_path, json={'source_sha256': mask['source_sha256'],
            'expected_source_revision': mask['source_revision'], 'expected_revision': mask['revision'],
            'spans': [{'start': start, 'end': start + len('Python')}]})
        assert saved.status_code == 200
        updated = client.get(endpoint).json()
        assert [item['text'] for item in updated['skill_candidates']['items']] == ['SQL']
        assert updated['mask_review_status'] == 'reviewed' and updated['mask_revision'] == 1
        assert 'Python' not in str(updated)
        original = recruitment.extract_skill_candidates
        def change_mask(source, structure):
            review = client.get(mask_path).json()
            changed = client.put(mask_path, json={'source_sha256': review['source_sha256'],
                'expected_source_revision': review['source_revision'], 'expected_revision': review['revision'], 'spans': []})
            assert changed.status_code == 200
            return original(source, structure)
        monkeypatch.setattr(recruitment, 'extract_skill_candidates', change_mask)
        assert client.get(endpoint).status_code == 409
        def change_and_restore(source, structure):
            with connect(app.state.database_path) as db:
                original_text = db.execute('SELECT resume_text FROM applications WHERE id=?', (application_id,)).fetchone()[0]
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('changed', application_id))
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', (original_text, application_id))
            return original(source, structure)
        monkeypatch.setattr(recruitment, 'extract_skill_candidates', change_and_restore)
        assert client.get(endpoint).status_code == 409


def test_session_revocation_during_extraction_denies_response(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'session.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username':'candidate','password':'candidate-demo-only'}).status_code == 200
        document = Document(); document.add_paragraph('Skills: Python, SQL')
        buf = BytesIO(); document.save(buf)
        application_id = client.post('/api/applications', params={'job_id':'job-backend-python','filename':'cv.docx'}, content=buf.getvalue()).json()['id']
        assert client.post('/api/auth/login', json={'username':'hr','password':'hr-demo-only'}).status_code == 200
        original = recruitment.extract_skill_candidates
        def revoke(source, structure):
            with connect(app.state.database_path) as db:
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
            return original(source, structure)
        monkeypatch.setattr(recruitment, 'extract_skill_candidates', revoke)
        assert client.get(f'/api/applications/{application_id}/resume/structured').status_code == 401
