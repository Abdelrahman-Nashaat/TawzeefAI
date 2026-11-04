"""Exercise graph screening in Streamlit through the real local API."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip('streamlit')
from streamlit.testing.v1 import AppTest

from app.db import connect
from app.main import create_app
from streamlit_client import LocalApi


@pytest.mark.parametrize('scenario', ['approved', 'changed', 'job_title_reverted', 'candidate_reverted', 'revoked', 'uncertain'])
def test_graph_screen_review_real_api(tmp_path, scenario):
    path = tmp_path / 'graph-screen.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as server:
        calls = []

        def forward(request):
            if request.url.path.endswith('/resume'):
                calls.append(request.url.path)
                if scenario == 'changed':
                    with connect(path) as db:
                        db.execute("UPDATE applications SET resume_text='Different' WHERE id=1")
                if scenario == 'job_title_reverted':
                    with connect(path) as db:
                        original = db.execute("SELECT title FROM jobs WHERE id='job-backend-python'").fetchone()[0]
                        db.execute("UPDATE jobs SET title='Temporary title' WHERE id='job-backend-python'")
                        db.execute("UPDATE jobs SET title=? WHERE id='job-backend-python'", (original,))
                if scenario == 'candidate_reverted':
                    with connect(path) as db:
                        original = db.execute('SELECT candidate_id FROM applications WHERE id=1').fetchone()[0]
                        other = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
                        db.execute('UPDATE applications SET candidate_id=? WHERE id=1', (other,))
                        db.execute('UPDATE applications SET candidate_id=? WHERE id=1', (original,))
                if scenario in ('revoked', 'uncertain'):
                    return httpx.Response(401 if scenario == 'revoked' else 503)
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
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=60)
            ui.session_state['local_api'] = hr
            ui.session_state['current_user'] = user
            ui.run()
            next(box for box in ui.selectbox if box.label == 'Review job').set_value('job-backend-python').run()
            next(button for button in ui.button if button.label == 'Start graph screening review').click().run()
            assert not ui.exception
            assert any(f'Candidate #{candidate}' in item.value and f'Application #{app_id}' in item.value for item in ui.text)
            review = hr.request('GET', f"/api/graph-reviews/{ui.session_state['graph_screen_review']['id']}")
            proposal = review['state']['action_proposal']
            assert proposal['job_id'] == 'job-backend-python'
            assert any(proposal['job_title'] in item.value for item in ui.text)
            assert ui.json
            next(button for button in ui.button if button.label == 'Approve and record graph screening').click().run()
            assert calls == []
            assert any('Confirm that you reviewed' in item.value for item in ui.error)
            next(box for box in ui.checkbox if box.label == 'I reviewed this graph screening').check()
            next(button for button in ui.button if button.label == 'Approve and record graph screening').click().run()
            assert not ui.exception
            assert len(calls) == 1
            with connect(path) as db:
                score = db.execute('SELECT score_json FROM applications WHERE id=?', (app_id,)).fetchone()[0]
                count = db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='screened'", (app_id,)).fetchone()[0]
            if scenario == 'approved':
                assert score is not None and count == 1
                assert any('Screening recorded locally' in item.value for item in ui.success)
            else:
                assert score is None and count == 0
                if scenario in ('changed', 'job_title_reverted', 'candidate_reverted'):
                    assert any('review changed' in item.value for item in ui.warning)
                elif scenario == 'revoked':
                    assert 'current_user' not in ui.session_state
                else:
                    assert any('could not be confirmed' in item.value for item in ui.warning)
                assert not any(button.label == 'Approve and record graph screening' for button in ui.button)
        finally:
            hr.close()
