"""Small independent synthetic evaluation; not a production accuracy benchmark."""

import pytest
from fastapi.testclient import TestClient

from app.auth import User
from app.db import connect
from app.main import create_app
from app.policy_agent import run_policy_agent


@pytest.fixture
def policy_corpus(tmp_path):
    path = tmp_path / 'policy-evaluation.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={
            'username': 'hr', 'password': 'hr-demo-only',
        }).status_code == 200
        with connect(path) as db:
            db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
            accounts = {row['role']: User(id=row['id'], username=row['username'],
                        display_name=row['display_name'], role=row['role'])
                        for row in db.execute("SELECT id,username,display_name,role FROM users WHERE username IN ('candidate','employee','hr')")}
        ids = {}
        for key, audience, collection, title, body in [
            ('public', 'public', 'handbook', 'Cobalt travel', 'Cobalt travel requests require a form.'),
            ('staff', 'staff', 'handbook', 'Saffron leave', 'Saffron leave requires manager approval.'),
            ('hr', 'hr', 'handbook', 'Marigold clearance', 'Marigold clearance requires a recruiter review.'),
            ('job', 'public', 'job_description', 'Python SQL role', 'Python SQL experience is needed.'),
            ('pattern', 'hr', 'past_hire_patterns', 'Python SQL profile', 'Python SQL experience featured previously.'),
            ('legacy', 'staff', 'legacy_unclassified', 'Tangerine allowance', 'Tangerine allowance is mentioned in an unclassified document.'),
        ]:
            response = client.post('/api/knowledge/documents/text', json={
                'title': title, 'audience': audience, 'collection': collection, 'text': body,
            })
            assert response.status_code == 201, response.text
            ids[key] = response.json()['id']
        yield path, accounts, ids


@pytest.mark.parametrize(('role', 'question', 'expected'), [
    ('candidate', 'cobalt travel', 'public'),
    ('employee', 'saffron leave', 'staff'),
    ('hr', 'marigold clearance', 'hr'),
])
def test_authorized_policy_is_retrieved(policy_corpus, role, question, expected):
    path, users, ids = policy_corpus
    result = run_policy_agent(path, users[role], question)
    assert result.answer.mode == 'extractive'
    assert [source.id for source in result.answer.sources] == [f'db-doc-{ids[expected]}-p1']
    assert result.tool_trace == ['search_policy']


@pytest.mark.parametrize(('role', 'question'), [
    ('candidate', 'saffron leave'),
    ('employee', 'marigold clearance'),
    ('hr', 'Python SQL policy'),
    ('employee', 'Tangerine allowance policy'),
    ('employee', 'nebula leave'),
])
def test_restricted_unsupported_and_nonpolicy_sources_abstain(policy_corpus, role, question):
    path, users, _ = policy_corpus
    result = run_policy_agent(path, users[role], question)
    assert result.answer.mode == 'abstain'
    assert result.answer.sources == []


def test_revocation_during_search_is_not_returned(policy_corpus):
    path, users, ids = policy_corpus
    checks = 0

    def authorize():
        nonlocal checks
        checks += 1
        if checks == 2:
            with connect(path) as db:
                db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (ids['staff'],))

    with pytest.raises(PermissionError):
        run_policy_agent(path, users['employee'], 'saffron leave', authorize=authorize)
    assert run_policy_agent(path, users['employee'], 'saffron leave').answer.mode == 'abstain'
