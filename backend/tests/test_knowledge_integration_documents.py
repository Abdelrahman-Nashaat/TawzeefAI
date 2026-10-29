from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_hr_documents_retrieve_only_for_authorized_roles_and_survive_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'documents.sqlite3'
    with TestClient(create_app(database_path=path, seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('candidate')
        assert client.post('/api/knowledge/documents/text', json={
            'title': 'Public nebula leave', 'audience': 'public', 'text': 'Public nebula leave is ten days.',
        }).status_code == 403
        login('hr')
        ids = {}
        for audience in ('public', 'staff', 'hr'):
            response = client.post('/api/knowledge/documents/text', json={
                'title': f'{audience} nebula leave', 'audience': audience,
                'text': f'{audience} nebula leave requires approval.',
            })
            assert response.status_code == 201
            ids[audience] = f"db-doc-{response.json()['id']}-p1"

        for role, allowed in [('candidate', {'public'}), ('employee', {'public', 'staff'}),
                              ('hr', {'public', 'staff', 'hr'})]:
            login(role)
            response = client.post('/api/knowledge/answer', json={'question': 'nebula leave'})
            assert response.status_code == 200
            source_ids = {source['id'] for source in response.json()['sources']}
            assert source_ids == {ids[audience] for audience in allowed}
        with connect(path) as db:
            events = db.execute('SELECT action FROM knowledge_document_audit').fetchall()
            assert [row['action'] for row in events] == ['created'] * 3

    with TestClient(create_app(database_path=path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={
            'username': 'employee', 'password': 'employee-demo-only',
        }).status_code == 200
        source_ids = {s['id'] for s in client.post('/api/knowledge/answer', json={
            'question': 'nebula leave',
        }).json()['sources']}
        assert source_ids == {ids['public'], ids['staff']}


def test_model_receives_only_authorized_documents(tmp_path, monkeypatch):
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    seen = []

    def fake_model(question, passages):
        seen.extend(passages)
        return f"Answer [{passages[0]['id']}]"

    monkeypatch.setattr('app.knowledge.generate_with_groq', fake_model)
    with TestClient(create_app(database_path=tmp_path / 'models.sqlite3', seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200
        login('hr')
        for audience in ('public', 'staff', 'hr'):
            assert client.post('/api/knowledge/documents/text', json={
                'title': f'{audience} nebula leave', 'audience': audience,
                'text': f'{audience} nebula leave requires approval.',
            }).status_code == 201
        login('employee')
        response = client.post('/api/knowledge/answer', json={'question': 'nebula leave'})
        assert response.status_code == 200 and response.json()['mode'] == 'groq'
        assert len(seen) == 2
        assert all('hr nebula leave' not in p['text'] for p in seen)


def test_long_document_retrieval_passes_late_matching_passage_to_model(tmp_path, monkeypatch):
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    sent = []

    def fake_model(question, passages):
        sent.extend(passages)
        return f"Answer [{passages[0]['id']}]"

    monkeypatch.setattr('app.knowledge.generate_with_groq', fake_model)
    with TestClient(create_app(database_path=tmp_path / 'long.sqlite3', seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={
            'username': 'hr', 'password': 'hr-demo-only',
        }).status_code == 200
        filler = ('Unrelated introduction about onboarding and attendance. ' * 110)
        response = client.post('/api/knowledge/documents/text', json={
            'title': 'Long handbook', 'audience': 'staff',
            'text': filler + 'Nebula sabbatical grants eleven paid days.',
        })
        assert response.status_code == 201
        doc_id = response.json()['id']
        assert client.post('/api/auth/login', json={
            'username': 'employee', 'password': 'employee-demo-only',
        }).status_code == 200
        result = client.post('/api/knowledge/answer', json={'question': 'nebula sabbatical'})
        assert result.status_code == 200 and result.json()['mode'] == 'groq'
        assert len(sent) == 1 and sent[0]['text'].endswith('Nebula sabbatical grants eleven paid days.')
        assert sent[0]['id'].startswith(f'db-doc-{doc_id}-p') and len(sent[0]['text']) <= 1600
        assert result.json()['sources'][0]['id'] == sent[0]['id']


def test_retrieval_includes_distinct_documents_before_repeating_long_document_chunks(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    with TestClient(create_app(database_path=tmp_path / 'diversity.sqlite3', seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        for text in ('quasar sabbatical policy approved. ' * 220, 'quasar sabbatical policy needs a supervisor.'):
            response = client.post('/api/knowledge/documents/text', json={
                'title': 'Quasar sabbatical', 'audience': 'staff', 'text': text,
            })
            assert response.status_code == 201
        result = client.post('/api/knowledge/answer', json={'question': 'quasar sabbatical policy'})
        ids = [source['id'] for source in result.json()['sources']]
        assert any(source.startswith('db-doc-1-') for source in ids)
        assert any(source.startswith('db-doc-2-') for source in ids)


def test_real_jobs_and_explicit_sample_policy_setting(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'settings.sqlite3'
    with TestClient(create_app(database_path=path, seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('hr')
        assert client.get('/api/knowledge/settings').json()['include_sample_policies'] is True
        with connect(path) as db:
            db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)', (
                'real-backend', 'Quasar Backend Engineer', 'Cairo', '["Python"]', '[]', '["build APIs"]',
            ))
        login('candidate')
        assert client.get('/api/knowledge/settings').status_code == 403
        assert client.post('/api/knowledge/settings', json={'include_sample_policies': False}).status_code == 403
        result = client.post('/api/knowledge/answer', json={'question': 'Quasar Backend Engineer'}).json()
        assert 'job-db-real-backend' in [source['id'] for source in result['sources']]
        login('hr')
        assert client.post('/api/knowledge/settings', json={'include_sample_policies': False}).status_code == 200
        # Toggling is explicit and independent of hidden-document existence.
        assert client.post('/api/knowledge/documents/text', json={
            'title': 'Private policy', 'audience': 'hr', 'text': 'The private policy is restricted.',
        }).status_code == 201
        login('employee')
        result = client.post('/api/knowledge/answer', json={'question': 'annual leave'}).json()
        assert result['mode'] == 'abstain'
        assert client.post('/api/knowledge/answer', json={'question': 'Quasar Backend Engineer'}).json()['sources']

    with TestClient(create_app(database_path=path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={
            'username': 'hr', 'password': 'hr-demo-only',
        }).status_code == 200
        assert client.get('/api/knowledge/settings').json()['include_sample_policies'] is False
        with connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM knowledge_settings_audit').fetchone()[0] == 1
