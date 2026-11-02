"""The HR interface records a decision only for the displayed snapshot."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.db import connect
from app.main import create_app
from streamlit_client import LocalApi


@pytest.mark.parametrize('scenario', ['reject', 'changed', 'uncertain', 'expired'])
def test_streamlit_hr_review_against_real_api(tmp_path, scenario):
    path = tmp_path / 'hr-ui.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as server:
        calls = []
        def forward(request):
            if request.url.path.endswith('/decision'):
                calls.append(request.url.path)
                if scenario == 'changed':
                    with connect(path) as db:
                        db.execute("UPDATE applications SET resume_text='Changed during review' WHERE id=1")
                if scenario in ('uncertain', 'expired'):
                    return httpx.Response(503 if scenario == 'uncertain' else 401)
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            user = hr.request('POST', '/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
            with connect(path) as db:
                candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
                app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','synthetic.pdf','Python SQL')", (candidate,)).lastrowid
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=30)
            ui.session_state['local_api'] = hr
            ui.session_state['current_user'] = user
            ui.run()
            assert not ui.exception
            next(box for box in ui.selectbox if box.label == 'Review job').set_value('job-backend-python').run()
            next(button for button in ui.button if button.label == 'Open masked review').click().run()
            assert not ui.exception
            assert any('Python SQL' in item.value for item in ui.text)
            decision = next(radio for radio in ui.radio if radio.label == 'Decision')
            decision.set_value('reject' if scenario == 'reject' else 'advance')
            if scenario == 'reject':
                next(button for button in ui.button if button.label == 'Record reviewed decision').click().run()
                assert any('Enter a reason' in item.value for item in ui.error)
                assert calls == []
                next(area for area in ui.text_area if area.label.startswith('Reason')).set_value('Synthetic review reason')
            next(button for button in ui.button if button.label == 'Record reviewed decision').click().run()
            assert not ui.exception
            assert calls == [f'/api/applications/{app_id}/decision']
            with connect(path) as db:
                state = db.execute('SELECT status FROM applications WHERE id=?', (app_id,)).fetchone()[0]
            if scenario == 'reject':
                assert state == 'rejected'
                assert any('Decision recorded' in item.value for item in ui.info)
            elif scenario == 'changed':
                assert state == 'submitted'
                assert any('expired or changed' in item.value for item in ui.info)
            elif scenario == 'uncertain':
                assert state == 'submitted'
                assert any('could not be confirmed' in item.value for item in ui.info)
            else:
                assert state == 'submitted'
                assert 'current_user' not in ui.session_state
        finally:
            hr.close()
