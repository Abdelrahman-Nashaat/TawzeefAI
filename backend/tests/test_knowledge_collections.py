from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app.db import connect, initialize
from app.knowledge_documents import fetch_authorized_documents, initialize_knowledge_documents
from app.main import create_app


def login(client, role):
    assert client.post('/api/auth/login', json={
        'username': role, 'password': f'{role}-demo-only',
    }).status_code == 200


def test_migrates_preexisting_documents_without_changing_visibility(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    initialize(path)
    with connect(path) as db:
        user = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES('hr','HR','hr','x')").lastrowid
        db.execute('''CREATE TABLE knowledge_documents (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, audience TEXT NOT NULL,
            filename TEXT, text TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT 'old')''')
        db.execute("INSERT INTO knowledge_documents(title,audience,text,created_by) VALUES('Old','public','Historic material',?)", (user,))
    initialize_knowledge_documents(path)
    initialize_knowledge_documents(path)
    records = fetch_authorized_documents(path, 'candidate')
    assert len(records) == 1 and records[0]['collection'] == 'legacy_unclassified'
    assert records[0]['text'] == 'Historic material'


def test_collection_ingestion_forces_hr_for_hire_patterns_and_persists_metadata(tmp_path):
    path = tmp_path / 'records.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        for collection in ('handbook', 'job_description', 'past_hire_patterns'):
            response = client.post('/api/knowledge/documents/text', json={
                'title': collection, 'audience': 'public', 'collection': collection,
                'text': f'{collection} synthetic searchable material',
            })
            assert response.status_code == 201
            assert response.json()['collection'] == collection
            assert response.json()['audience'] == ('hr' if collection == 'past_hire_patterns' else 'public')
        old = client.post('/api/knowledge/documents/text', json={
            'title': 'legacy', 'audience': 'public', 'text': 'Legacy source',
        })
        assert old.json()['collection'] == 'legacy_unclassified'
        assert client.post('/api/knowledge/documents/text', json={
            'title': 'bad', 'audience': 'public', 'collection': 'invalid', 'text': 'Synthetic',
        }).status_code == 422
        doc = Document()
        doc.add_paragraph('Synthetic historic hiring pattern')
        buffer = BytesIO()
        doc.save(buffer)
        uploaded = client.post('/api/knowledge/documents', params={
            'title': 'hire file', 'audience': 'staff', 'filename': 'synthetic.docx',
            'collection': 'past_hire_patterns',
        }, content=buffer.getvalue())
        assert uploaded.status_code == 201
        assert uploaded.json()['audience'] == 'hr'
        assert uploaded.json()['collection'] == 'past_hire_patterns'
        assert all('collection' in row for row in client.get('/api/knowledge/documents').json())
        assert 'past_hire_patterns' not in {r['collection'] for r in fetch_authorized_documents(path, 'candidate')}
        assert 'past_hire_patterns' not in {r['collection'] for r in fetch_authorized_documents(path, 'employee')}
        assert len([r for r in fetch_authorized_documents(path, 'hr') if r['collection'] == 'past_hire_patterns']) == 2


def test_past_hire_protection_and_history_after_direct_reclassification(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'history.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Quasar onboarding', 'audience': 'public', 'collection': 'handbook',
            'text': 'Quasar onboarding is held every Monday.',
        }).json()
        login(client, 'employee')
        conversation = client.post('/api/conversations', json={'title': 'test'}).json()['id']
        endpoint = f'/api/conversations/{conversation}/messages'
        assert client.post(endpoint, json={'question': 'quasar onboarding'}).json()['sources']
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET collection='past_hire_patterns', audience='public' WHERE id=?", (doc['id'],))
        assert fetch_authorized_documents(path, 'employee') == []
        assert fetch_authorized_documents(path, 'candidate') == []
        assert len(fetch_authorized_documents(path, 'hr')) == 1
        new_answer = client.post('/api/knowledge/answer', json={'question': 'quasar onboarding'}).json()
        assert new_answer['sources'] == []
        stored = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert stored['mode'] == 'unavailable' and stored['sources'] == []


def test_document_job_metadata_validation_and_legacy_default(tmp_path):
    path = tmp_path / 'job-documents.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        payload = {'title': 'Job guide', 'audience': 'public', 'text': 'Synthetic guide',
                   'collection': 'job_description', 'job_id': 'job-backend-python'}
        created = client.post('/api/knowledge/documents/text', json=payload)
        assert created.status_code == 201
        assert created.json()['job_id'] == 'job-backend-python'
        assert client.get('/api/knowledge/documents').json()[0]['job_id'] == 'job-backend-python'
        assert client.post('/api/knowledge/documents/text', json={**payload, 'job_id': 'does-not-exist'}).status_code == 422
        assert client.post('/api/knowledge/documents/text', json={**payload, 'job_id': 'BAD ID'}).status_code == 422
        assert client.post('/api/knowledge/documents/text', json={**payload, 'job_id': None}).json()['job_id'] is None
        doc = Document()
        doc.add_paragraph('Synthetic job document')
        buffer = BytesIO()
        doc.save(buffer)
        response = client.post('/api/knowledge/documents', params={
            'title': 'Job file', 'audience': 'public', 'filename': 'job.docx',
            'job_id': 'job-backend-python',
        }, content=buffer.getvalue())
        assert response.status_code == 201 and response.json()['job_id'] == 'job-backend-python'
        rejected = client.post('/api/knowledge/documents', params={
            'title': 'Missing job', 'audience': 'public', 'filename': 'job.docx',
            'job_id': 'does-not-exist',
        }, content=buffer.getvalue())
        assert rejected.status_code == 422
