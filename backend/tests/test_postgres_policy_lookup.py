"""Single-topic policy lookup remains role-filtered and ambiguity-safe."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import login, mode  # noqa: F401

from app.main import create_app


def test_policy_lookup_requires_a_unique_authorized_title_and_revalidates(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')

        def create(title, text, audience='public'):
            result = hr.post('/api/knowledge/documents/text', json={'title': title, 'text': text,
                             'audience': audience, 'collection': 'handbook'})
            assert result.status_code == 201
            return result.json()['id']

        public = create('Synthetic leave policy', 'The synthetic leave policy allows five days.')
        create('Private leave policy', 'Confidential leave policy details.', 'hr')
        create('Holiday handbook', 'Holiday leave policy is unrelated.')
        question = {'question': 'What is the leave policy?'}
        answer = candidate.post('/api/knowledge/answer', json=question).json()
        assert answer['mode'] == 'extractive'
        assert [s['title'] for s in answer['sources']] == ['Synthetic leave policy']
        assert 'Confidential' not in str(answer)
        assert hr.post('/api/knowledge/answer', json=question).json()['mode'] == 'abstain'
        for query in ('leave', 'policy', 'holiday policy', 'zebra policy', 'leaves policy', 'leaves policies'):
            assert candidate.post('/api/knowledge/answer', json={'question': query}).json()['mode'] == 'abstain'

        cid = candidate.post('/api/knowledge/conversations').json()['id']
        turn = candidate.post(f'/api/knowledge/conversations/{cid}/turns', json=question)
        assert turn.status_code == 201
        assert [s['title'] for s in turn.json()['sources']] == ['Synthetic leave policy']
        assert candidate.get(f'/api/knowledge/conversations/{cid}/turns').json()['items'][0]['mode'] == 'extractive'

        other = create('Other leave policy', 'Other leave rules.')
        assert candidate.post('/api/knowledge/answer', json=question).json()['mode'] == 'abstain'
        assert candidate.post(f'/api/knowledge/conversations/{cid}/turns', json=question).json()['mode'] == 'abstain'
        assert hr.post(f'/api/knowledge/documents/{other}/deactivate').status_code == 200
        assert hr.post(f'/api/knowledge/documents/{public}/deactivate').status_code == 200
        assert candidate.post('/api/knowledge/answer', json=question).json()['mode'] == 'abstain'
        first = candidate.get(f'/api/knowledge/conversations/{cid}/turns').json()['items'][0]
        assert first['mode'] == 'source_unavailable' and first['sources'] == []
