"""Role- and job-scoped knowledge in resumable specialist reviews."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_postgres_durable_graph import assess, create_application
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_scheduling import wait_for_lock

from app.main import create_app
from app.postgres_graph_review import _snapshot


def add_doc(client, *, title, collection, audience='hr', job=None):
    payload = {'title': title, 'text': 'Python deployment production checklist.',
               'collection': collection, 'audience': audience, 'page': 2}
    if job:
        payload['job_id'] = job
    response = client.post('/api/knowledge/documents/text', json=payload)
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_resumable_review_uses_job_scoped_collections_and_detects_revocation(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        app = next(item for item in hr.get('/api/applications').json() if item['id'] == app_id)
        job = app['job_id']
        add_doc(hr, title='General policy', collection='handbook', audience='public')
        add_doc(hr, title='Role specification', collection='job_description', job=job)
        pattern = add_doc(hr, title='Role patterns', collection='past_hire_patterns', job=job)
        add_doc(hr, title='Other role patterns', collection='past_hire_patterns', job='different-role')
        add_doc(hr, title='Unassigned patterns', collection='past_hire_patterns')
        misconfigured = add_doc(hr, title='Misconfigured pattern', collection='past_hire_patterns', job=job)
        with db_for(mode) as db:
            # The API prohibits this, but imported or directly edited records
            # must not be treated as authorized past-hire patterns either.
            db.execute("UPDATE knowledge_documents SET audience='public' WHERE id=%s", (misconfigured,))
        url = f'/api/applications/{app_id}/graph-review-preparations'
        assert candidate.post(url, json={'policy_question': 'Python deployment'}).status_code == 403
        response = hr.post(url, json={'policy_question': 'Python deployment'})
        assert response.status_code == 201, response.text
        draft = response.json()['id']
        opened = hr.get(f'/api/graph-review-drafts/{draft}').json()
        sources = opened['result']['policy']['sources']
        assert {source['collection'] for source in sources} == {'handbook', 'job_description', 'past_hire_patterns'}
        assert {source['title'] for source in sources} == {'General policy', 'Role specification', 'Role patterns'}
        assert all(source['page'] == 2 for source in sources)
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        assert hr.post(f'/api/knowledge/documents/{pattern}/deactivate').status_code == 200
        assert hr.get(f'/api/graph-review-drafts/{draft}').json()['stale'] is True
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 409
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': False}).status_code == 200


def test_legacy_v4_review_preserves_handbook_only_fingerprint(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        app = next(item for item in hr.get('/api/applications').json() if item['id'] == app_id)
        add_doc(hr, title='General policy', collection='handbook', audience='public')
        add_doc(hr, title='Role patterns', collection='past_hire_patterns', job=app['job_id'])
        response = hr.post(f'/api/applications/{app_id}/graph-review-preparations',
                           json={'policy_question': 'Python deployment'})
        assert response.status_code == 201, response.text
        draft = response.json()['id']
        with db_for(mode) as db:
            _, old_fingerprint, _ = _snapshot(db, app_id, 'Python deployment')
            db.execute('UPDATE graph_review_drafts SET checkpoint_version=4,fingerprint=%s WHERE id=%s',
                       (old_fingerprint, draft))
            # The checkpoint itself must carry the same immutable fingerprint;
            # legacy reviews produced by v4 have that value from creation.
            db.execute("UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{channel_values,fingerprint}', to_jsonb(%s::text)) WHERE thread_id=%s",
                       (old_fingerprint, f'graph-review-{draft}'))
        opened = hr.get(f'/api/graph-review-drafts/{draft}')
        assert opened.status_code == 200, opened.text
        assert not opened.json()['stale']
        assert [source['title'] for source in opened.json()['result']['policy']['sources']] == ['General policy']
        assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 200


@pytest.mark.parametrize('resumable', [False, True])
def test_knowledge_insert_serializes_with_approval(mode, monkeypatch, resumable):  # noqa: F811
    import app.postgres_graph_review as graph

    entered, release = Event(), Event()
    original = graph._resume_approval

    def pause(*args, **kwargs):
        entered.set()
        if not release.wait(20):
            raise TimeoutError('Approval was not released')
        return original(*args, **kwargs)

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as writer:
        app_id = create_application(candidate)
        login(hr, 'hr')
        login(writer, 'hr')
        assess(hr, app_id)
        add_doc(hr, title='Policy', collection='handbook', audience='public')
        kind = 'graph-review-preparations' if resumable else 'graph-review-drafts'
        response = hr.post(f'/api/applications/{app_id}/{kind}',
                           json={'intent': 'advance', 'policy_question': 'Python deployment'})
        assert response.status_code == 201, response.text
        draft = response.json()['id']
        if resumable:
            assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        monkeypatch.setattr(graph, '_resume_approval', pause)
        with ThreadPoolExecutor(max_workers=2) as pool:
            approval = pool.submit(hr.post, f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True})
            try:
                assert entered.wait(20), 'Approval did not reach checkpoint resumption'
                insertion = pool.submit(add_doc, writer, title='New matching policy',
                                        collection='handbook', audience='public')
                wait_for_lock(mode, 'advisory')
                assert not insertion.done()
            finally:
                release.set()
            assert approval.result(timeout=20).status_code == 200
            assert isinstance(insertion.result(timeout=20), int)
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'advanced'


@pytest.mark.parametrize('resumable', [False, True])
def test_new_matching_source_before_approval_makes_review_stale(mode, resumable):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        assess(hr, app_id)
        add_doc(hr, title='Policy', collection='handbook', audience='public')
        kind = 'graph-review-preparations' if resumable else 'graph-review-drafts'
        response = hr.post(f'/api/applications/{app_id}/{kind}',
                           json={'intent': 'advance', 'policy_question': 'Python deployment'})
        assert response.status_code == 201, response.text
        draft = response.json()['id']
        if resumable:
            assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
        add_doc(hr, title='New matching policy', collection='handbook', audience='public')
        assert hr.post(f'/api/graph-review-drafts/{draft}/resume', json={'reviewed': True}).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (app_id,)).fetchone()['status'] == 'submitted'
