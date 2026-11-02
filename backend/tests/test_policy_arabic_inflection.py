"""Synthetic policy questions with Arabic grammatical inflections."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.policy_agent import _has_explicit_negation


@pytest.fixture
def policy_client(tmp_path, monkeypatch):
    for name in ('TAWZEEF_ENABLE_GROQ', 'TAWZEEF_USE_POLICY_HYBRID',
                 'TAWZEEF_POLICY_HYBRID_ANSWERS', 'TAWZEEF_POLICY_HYBRID_SUGGESTIONS'):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(tmp_path / 'arabic-policy.sqlite3', seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': role + '-demo-only',
            }).status_code == 200

        login('hr')
        sources = [
            ('Maintenance', 'يرسل الموظف تقرير الصيانة يوم الخميس.', 'staff'),
            ('Inventory', 'يراجع المشرف سجل المخزون يوم السبت.', 'staff'),
            ('Restricted training', 'يرسل الموظف ملف التدريب إلى المدير.', 'hr'),
            ('Permits', 'لا يوافق المدير على اعتماد التصريح يوم الخميس.', 'staff'),
            ('Training', 'Training requests are approved on Monday.', 'staff'),
            ('Overtime', 'Overtime requests are not approved on Mondays.', 'staff'),
            ('Eligibility', 'Annual leave eligibility requires six months of service.', 'staff'),
        ]
        for title, text, audience in sources:
            response = client.post('/api/knowledge/documents/text', json={
                'title': title, 'text': text, 'audience': audience, 'collection': 'handbook',
            })
            assert response.status_code == 201

        login('employee')
        chat = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']

        def ask(question, chat_id=None):
            response = client.post(f'/api/conversations/{chat_id or chat}/messages', json={
                'question': question, 'mode': 'policy',
            })
            assert response.status_code == 201
            return response.json()

        yield client, login, ask


@pytest.mark.xfail(strict=True, reason='Local policy retrieval misses Arabic verb inflections')
def test_arabic_policy_inflections(policy_client):
    _, _, ask = policy_client
    first = ask('متى أرسل تقرير الصيانة؟')
    assert first['mode'] == 'extractive'
    assert 'يرسل الموظف تقرير الصيانة يوم الخميس' in first['answer']
    second = ask('متى أراجع سجل المخزون؟')
    assert second['mode'] == 'extractive'
    assert 'يراجع المشرف سجل المخزون يوم السبت' in second['answer']


def test_arabic_policy_scope_and_access(policy_client):
    client, login, ask = policy_client
    for question in ('متى أرسل تقرير الصيانة السنوية؟',
                     'متى لا أرسل تقرير الصيانة؟',
                     'متى أرسل ملف التدريب؟'):
        result = ask(question)
        assert result['mode'] == 'abstain'
        assert result['sources'] == []

    login('candidate')
    chat = client.post('/api/conversations', json={'title': 'Candidate'}).json()['id']
    result = ask('متى أرسل تقرير الصيانة؟', chat)
    assert result['mode'] == 'abstain'
    assert result['sources'] == []


def test_positive_eligibility_question_still_finds_policy(policy_client):
    _, _, ask = policy_client
    result = ask('What is annual leave eligibility?')
    assert result['mode'] == 'extractive'
    assert 'six months of service' in result['answer']


def test_negated_question_and_negated_policy_do_not_match_opposite_polarity(policy_client):
    _, _, ask = policy_client
    for question in ('متى لا يرسل تقرير الصيانة؟', 'متى يوافق المدير اعتماد التصريح؟',
                     'متى لا يوافق المدير اعتماد التصريح؟',
                     'When are training requests not approved?',
                     'When are overtime requests approved on Mondays?',
                     'When are overtime requests not approved on Mondays?'):
        result = ask(question)
        assert result['mode'] == 'abstain'
        assert result['sources'] == []
        assert 'Negation' in result['notice']


@pytest.mark.parametrize('text', ['no', 'not', 'never', 'without', "isn't", 'لا', 'لم', 'لن', 'ليس', 'غير'])
def test_explicit_negators_are_not_discarded(text):
    assert _has_explicit_negation(text)


@pytest.mark.parametrize('text', ['notice', 'annual eligibility', 'entitlement', 'استحقاق الاجازة'])
def test_ordinary_words_do_not_trigger_negation(text):
    assert not _has_explicit_negation(text)
