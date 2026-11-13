"""Read-only model plans use bounded facts and are revalidated after latency."""
import json

import pytest
from fastapi.testclient import TestClient
from test_postgres_durable_graph import assess, create_application
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def ready_review(hr, candidate):
    app_id = create_application(candidate)
    login(hr, 'hr')
    assess(hr, app_id)
    response = hr.post(f'/api/applications/{app_id}/graph-review-preparations', json={})
    assert response.status_code == 201, response.text
    draft = response.json()['id']
    assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
    return app_id, draft


def test_suggestion_is_explicit_bounded_and_does_not_decide(mode, monkeypatch):  # noqa: F811
    import app.postgres_graph_review as graph
    calls = []
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    monkeypatch.setattr(graph, 'request_plan', lambda facts, options: calls.append((facts, options)) or 'review_rubric')
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, draft = ready_review(hr, candidate)
        assert candidate.post(f'/api/graph-review-drafts/{draft}/ai-suggestion').status_code == 403
        assert not calls
        response = hr.post(f'/api/graph-review-drafts/{draft}/ai-suggestion')
        assert response.status_code == 200, response.text
        assert response.json()['suggested_step'] == 'review_rubric'
        assert calls == [({'application_status': 'submitted', 'assessment_status': 'current',
                           'interview_status': None, 'has_available_slot': False}, ['review_rubric', 'review_advance'])]
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft,)).fetchone()['status'] == 'pending'


def test_plan_rejects_unallowed_or_stale_results(mode, monkeypatch):  # noqa: F811
    import app.postgres_graph_review as graph
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, draft = ready_review(hr, candidate)
        monkeypatch.setattr(graph, 'request_plan', lambda *_: 'hire')
        assert hr.post(f'/api/graph-review-drafts/{draft}/ai-suggestion').status_code == 503
        def stale(*_):
            with db_for(mode) as db:
                db.execute("UPDATE applications SET resume_text=resume_text || ' changed' WHERE id=%s", (app_id,))
            return 'review_rubric'
        monkeypatch.setattr(graph, 'request_plan', stale)
        assert hr.post(f'/api/graph-review-drafts/{draft}/ai-suggestion').status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'


def test_session_expiry_during_model_call_does_not_disclose_suggestion(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_experimental as pg_app
    import app.postgres_graph_review as graph

    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id, draft = ready_review(hr, candidate)
        def expire(*_):
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 86400))
            return 'review_rubric'
        monkeypatch.setattr(graph, 'request_plan', expire)
        response = hr.post(f'/api/graph-review-drafts/{draft}/ai-suggestion')
        assert response.status_code == 401 and 'suggested_step' not in response.text
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
            assert db.execute('SELECT status FROM graph_review_drafts WHERE id=%s', (draft,)).fetchone()['status'] == 'pending'


@pytest.mark.parametrize('content', ['{}', '{"action":"hire"}', '{"action":"review_rubric","extra":1}', 'not-json'])
def test_provider_response_must_be_exact_allowed_action(monkeypatch, content):
    import app.postgres_ai_planner as planner
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _): return json.dumps({'choices': [{'message': {'content': content}}]}).encode()
    monkeypatch.setattr(planner, '_open', lambda _: Response())
    with pytest.raises(planner.ProviderUnavailable):
        planner.request_plan({'application_status': 'submitted'}, ['review_rubric'])
