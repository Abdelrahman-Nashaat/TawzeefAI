"""Policy evidence in supervised Streamlit actions via the actual API."""
from pathlib import Path

import httpx
import pytest

pytest.importorskip('streamlit')
from streamlit.testing.v1 import AppTest

from app.db import connect
from streamlit_client import LocalApi


@pytest.mark.parametrize('intent,scenario', [('present_offer', 'answer'), ('reject_application', 'stale'),
                                            ('reject_application', 'abstain')])
def test_policy_evidence_and_review(case, intent, scenario):
    client, path, application, _, _, _ = case
    policy = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days. synthetic-policy-evidence',
    }).json()['id']

    def forward(request):
        client.cookies.clear()
        response = client.request(request.method, request.url.raw_path.decode(),
                                  headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    hr = LocalApi(8000, transport=httpx.MockTransport(forward))
    try:
        user = hr.request('POST', '/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
        ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=60)
        ui.session_state['local_api'] = hr
        ui.session_state['current_user'] = user
        ui.run()
        next(box for box in ui.selectbox if box.label == 'Review job').set_value('job-backend-python').run()
        prefix = 'offer' if intent == 'present_offer' else 'rejection'
        field = 'Terms for graph offer' if intent == 'present_offer' else 'Reason for graph rejection'
        next(area for area in ui.text_area if area.label == field).set_value('Synthetic input')
        question = 'What is quasar leave?' if scenario != 'abstain' else 'What is zzznonexistentpolicy?'
        next(area for area in ui.text_area if area.label == f'Optional policy question for graph {prefix}').set_value(question)
        next(button for button in ui.button if button.label == f'Start graph {prefix} review').click().run()
        assert not ui.exception
        if scenario != 'abstain':
            assert any('synthetic-policy-evidence' in item.value for item in ui.text)
            assert any('Quasar policy' in item.value for item in ui.text)
        else:
            assert any('No policy answer was found' in item.value for item in ui.warning)
        state_key = f"graph_{'offer' if intent == 'present_offer' else 'reject'}_policy_{application}"
        assert state_key not in ui.session_state
        if scenario == 'stale':
            with connect(path) as db:
                db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (policy,))
            ui.run()
            assert not any(button.label == f'Approve graph {prefix}' for button in ui.button)
            assert any('Policy sources changed' in item.value for item in ui.warning)
            next(button for button in ui.button if button.label == f'Discard graph {prefix} review').click().run()
        elif scenario == 'answer':
            next(box for box in ui.checkbox if box.label == 'I reviewed the candidate, role and offer terms').check()
            next(button for button in ui.button if button.label == 'Approve graph offer').click().run()
            assert any('Offer presented locally' in item.value for item in ui.success)
        else:
            next(box for box in ui.checkbox if box.label == 'I reviewed the rejection reason and effects').check()
            next(button for button in ui.button if button.label == 'Approve graph rejection').click().run()
            assert any('Rejection recorded locally' in item.value for item in ui.success)
        with connect(path) as db:
            status = db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0]
        assert status == {'stale': 'advanced', 'answer': 'offered', 'abstain': 'rejected'}[scenario]
    finally:
        hr.close()


pytest_plugins = ('test_graph_offer',)
