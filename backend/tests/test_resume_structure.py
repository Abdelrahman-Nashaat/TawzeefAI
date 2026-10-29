from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.main import create_app
from app.resume_structure import structure_resume


def test_sections_are_verbatim_linked_and_unrecognized_information_is_unknown():
    text = 'Skills: Python, SQL\nExperience\nBuilt services for 2 years\nالتعليم: بكالوريوس حاسبات\nProjects\nAPI project\nLanguages\nEnglish'
    result = structure_resume(text)
    assert result['sections']['skills']['items'][0]['text'] == 'Python, SQL'
    assert result['sections']['education']['items'][0]['text'] == 'بكالوريوس حاسبات'
    for section in result['sections'].values():
        for item in section['items']:
            assert text[item['start']:item['end']] == item['text']
    unknown = structure_resume('I have experience with Python but no section headings.')
    assert all(section['status'] == 'unknown' for section in unknown['sections'].values())
    assert all(not section['items'] for section in unknown['sections'].values())


def test_does_not_guess_missing_information_or_cross_unrelated_headings():
    result = structure_resume('المهارات\nPython\nPersonal details:\nPrivate identity\nالخبرة\n')
    assert [item['text'] for item in result['sections']['skills']['items']] == ['Python']
    assert result['sections']['experience']['status'] == 'unknown'
    assert 'Private identity' not in str(result)
    assert result['requires_human_review'] is True


def test_unicode_offsets_and_bounded_extracts_are_explicit():
    text = 'Projects:\n🚀 مشروع تجريبي\n' + ('x' * 1100)
    result = structure_resume(text)
    section = result['sections']['projects']
    assert section['truncated'] is True
    assert len(section['items'][1]['text']) == 1000
    for item in section['items']:
        assert text[item['start']:item['end']] == item['text']


def test_markdown_headings_and_bold_labels_keep_exact_offsets_and_stop_at_unknown_heading():
    text = ('# Skills\r\n- Python\r\n'
            '**الخبرة:** بناء APIs\r\n'
            '## Other personal information\r\nSensitive details\r\n'
            '**Projects**: 📦 Portfolio\r\n'
            '- **Education:** Computer Science\r\n')
    sections = structure_resume(text)['sections']
    assert [x['text'] for x in sections['skills']['items']] == ['- Python']
    assert [x['text'] for x in sections['experience']['items']] == ['بناء APIs']
    assert [x['text'] for x in sections['projects']['items']] == ['📦 Portfolio']
    assert [x['text'] for x in sections['education']['items']] == ['Computer Science']
    assert 'Sensitive details' not in str(sections)
    for section in sections.values():
        for item in section['items']:
            assert text[item['start']:item['end']] == item['text']


def test_structure_endpoint_is_hr_only_and_uses_same_redacted_source(tmp_path):
    with TestClient(create_app(tmp_path / 'test.sqlite3', seed_demo=True)) as client:
        def login(username, password):
            assert client.post('/api/auth/login', json={'username': username, 'password': password}).status_code == 200

        login('candidate', 'candidate-demo-only')
        document = Document()
        for line in ['Name: Synthetic Person', 'Skills:', 'Python, SQL', 'التعليم: حاسبات']:
            document.add_paragraph(line)
        data = BytesIO()
        document.save(data)
        app_id = client.post('/api/applications', params={'job_id': 'job-backend-python', 'filename': 'cv.docx'}, content=data.getvalue()).json()['id']
        endpoint = f'/api/applications/{app_id}/resume/structured'
        assert client.get(endpoint).status_code == 403
        with connect(client.app.state.database_path) as db:
            db.execute('INSERT INTO users(username, display_name, role, password_hash) VALUES (?, ?, ?, ?)',
                       ('other', 'Other', 'candidate', hash_password('other-pass')))
        login('other', 'other-pass')
        assert client.get(endpoint).status_code == 403
        login('employee', 'employee-demo-only')
        assert client.get(endpoint).status_code == 403
        login('hr', 'hr-demo-only')
        response = client.get(endpoint)
        assert response.status_code == 200
        result = response.json()
        source = client.get(f'/api/applications/{app_id}/rubric/source').json()['text']
        assert 'Synthetic Person' not in str(result)
        for section in result['sections'].values():
            for item in section['items']:
                assert source[item['start']:item['end']] == item['text']
        assert client.get('/api/applications/999999/resume/structured').status_code == 404
