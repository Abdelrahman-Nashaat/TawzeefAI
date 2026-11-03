"""Graph responses must not reveal content invalidated during rendering."""
import pytest
from fastapi.testclient import TestClient

from app import graph_orchestrator, policy_agent
from app.db import connect
from app.main import create_app

MARKER = 'SyntheticSecretRole982'
SOURCE = f'Experience\nRole: {MARKER}\nCompany: Fictional Co'


def login(client):
    assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200


@pytest.mark.parametrize('change', ['source', 'source_revert', 'mask', 'mask_revert', 'rubric'])
def test_full_view_rejects_fresh_source_mask_or_evidence_change(tmp_path, monkeypatch, change):
    path = tmp_path / 'graph-fresh.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client, TestClient(app) as other:
        login(client)
        login(other)
        with connect(path) as db:
            assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            appid = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','fictional.pdf',?)", (candidate, SOURCE)).lastrowid
        result = client.post('/api/graph-reviews', json={'application_id': appid, 'intent': 'full'})
        assert result.status_code == 201 and MARKER in result.text
        review_id = result.json()['id']
        original = graph_orchestrator._require_current_role
        def change_then_authorize(db, request, user, role):
            if change.startswith('source'):
                with connect(path) as writer:
                    writer.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Experience\nRole: New role', appid))
                    if change == 'source_revert':
                        writer.execute('UPDATE applications SET resume_text=? WHERE id=?', (SOURCE, appid))
            elif change.startswith('mask'):
                endpoint = f'/api/applications/{appid}/resume/mask'
                mask = other.get(endpoint).json()
                start = mask['source'].index(MARKER)
                saved = other.put(endpoint, json={'source_sha256': mask['source_sha256'], 'expected_source_revision': mask['source_revision'], 'expected_revision': mask['revision'], 'spans': [{'start': start, 'end': start + len(MARKER)}]})
                assert saved.status_code == 200, saved.text
                if change == 'mask_revert':
                    current = other.get(endpoint).json()
                    saved = other.put(endpoint, json={'source_sha256': current['source_sha256'], 'expected_source_revision': current['source_revision'], 'expected_revision': current['revision'], 'spans': []})
                    assert saved.status_code == 200, saved.text
            else:
                response = other.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'mandatory', 'label': 'Fictional criterion'})
                assert response.status_code == 201, response.text
            return original(db, request, user, role)
        monkeypatch.setattr(graph_orchestrator, '_require_current_role', change_then_authorize)
        response = client.get(f'/api/graph-reviews/{review_id}')
        assert response.status_code == 409
        assert MARKER not in response.text


@pytest.mark.parametrize('change', ['content', 'scope', 'inactive'])
def test_policy_excerpt_is_rechecked_after_view_snapshot(tmp_path, monkeypatch, change):
    path = tmp_path / 'policy-fresh.sqlite3'
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'fictional-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda question, key: {'tool_calls': [{'type': 'function', 'function': {'name': 'search_policy', 'arguments': '{"query":"quasar leave"}'}}]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda question, passages: f"answer [{passages[0]['id']}]")
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client)
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            appid = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','fictional.pdf','synthetic')", (candidate,)).lastrowid
        document = client.post('/api/knowledge/documents/text', json={'title': 'Quasar leave', 'audience': 'hr', 'collection': 'handbook', 'text': f'Quasar leave is eleven days. {MARKER}'})
        assert document.status_code == 201, document.text
        doc_id = document.json()['id']
        result = client.post('/api/graph-reviews', json={'application_id': appid, 'intent': 'policy', 'policy_question': 'What is quasar leave?'})
        assert result.status_code == 201 and MARKER in result.text
        original = graph_orchestrator._require_current_role
        def change_then_authorize(db, request, user, role):
            with connect(path) as writer:
                if change == 'content':
                    writer.execute('UPDATE knowledge_documents SET text=? WHERE id=?', ('Quasar leave changed.', doc_id))
                elif change == 'scope':
                    writer.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (doc_id,))
                else:
                    writer.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc_id,))
            return original(db, request, user, role)
        monkeypatch.setattr(graph_orchestrator, '_require_current_role', change_then_authorize)
        response = client.get(f"/api/graph-reviews/{result.json()['id']}")
        assert response.status_code == 409
        assert MARKER not in response.text
