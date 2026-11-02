"""Synthetic policy questions with Arabic grammatical inflections."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.xfail(strict=True, reason='Local policy retrieval misses Arabic verb inflections')
def test_arabic_policy_inflections_and_scope(tmp_path, monkeypatch):
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
        ]
        for title, text, audience in sources:
            response = client.post('/api/knowledge/documents/text', json={
                'title': title, 'text': text, 'audience': audience, 'collection': 'handbook',
            })
            assert response.status_code == 201

        login('employee')
        chat = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']

        def ask(question):
            response = client.post(f'/api/conversations/{chat}/messages', json={
                'question': question, 'mode': 'policy',
            })
            assert response.status_code == 201
            return response.json()

        first = ask('متى أرسل تقرير الصيانة؟')
        assert first['mode'] == 'extractive'
        assert 'يرسل الموظف تقرير الصيانة يوم الخميس' in first['answer']
        second = ask('متى أراجع سجل المخزون؟')
        assert second['mode'] == 'extractive'
        assert 'يراجع المشرف سجل المخزون يوم السبت' in second['answer']
        for question in ('متى أرسل تقرير الصيانة السنوية؟',
                         'متى لا أرسل تقرير الصيانة؟',
                         'متى أرسل ملف التدريب؟'):
            result = ask(question)
            assert result['mode'] == 'abstain'
            assert result['sources'] == []

        login('candidate')
        chat = client.post('/api/conversations', json={'title': 'Candidate'}).json()['id']
        result = ask('متى أرسل تقرير الصيانة؟')
        assert result['mode'] == 'abstain'
        assert result['sources'] == []
