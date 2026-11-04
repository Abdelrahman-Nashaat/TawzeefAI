"""Exercise interview cancellation and HR inbox in the local Streamlit app."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip('streamlit')
from streamlit.testing.v1 import AppTest

from app.db import connect
from app.main import create_app
from streamlit_client import LocalApi


@pytest.fixture
def cancellation_case(tmp_path):
    path = tmp_path / 'cancellations.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as server:
        def forward(request):
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        candidate = LocalApi(8000, transport=httpx.MockTransport(forward))
        user = candidate.request('POST', '/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'})
        hr_user = hr.request('POST', '/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
        with connect(path) as db:
            candidate_id = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','synthetic','advanced')", (candidate_id,)).lastrowid
        start = datetime.now(UTC) + timedelta(days=3)
        slot = hr.request('POST', '/api/slots', json={'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Synthetic interviewer'})
        interview = candidate.request('POST', f"/api/slots/{slot['id']}/book", json={'application_id': application})
        yield path, hr, hr_user, candidate, user, application, interview
        candidate.close()
        hr.close()


def ui_for(api, user):
    ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=60)
    ui.session_state['local_api'] = api
    ui.session_state['current_user'] = user
    return ui.run()


def test_candidate_cancels_and_hr_acknowledges_in_streamlit(cancellation_case):
    path, hr, hr_user, candidate, user, application, interview = cancellation_case
    ui = ui_for(candidate, user)
    assert not ui.exception
    assert not any('Interview cancellation notices' in item.value for item in ui.subheader)
    next(item for item in ui.button if item.label == 'Cancel interview').click().run()
    assert any('Confirm the cancellation' in item.value for item in ui.error)
    assert candidate.request('GET', '/api/interviews')[0]['status'] == 'scheduled'
    next(item for item in ui.checkbox if item.label == 'I want to cancel this interview').check()
    next(item for item in ui.button if item.label == 'Cancel interview').click().run()
    assert not ui.exception
    assert any('Interview cancelled locally' in item.value for item in ui.success)
    assert candidate.request('GET', '/api/interviews')[0]['status'] == 'cancelled'
    assert candidate.request('GET', '/api/slots', params={'application_id': application})
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0
    hr_ui = ui_for(hr, hr_user)
    assert not hr_ui.exception
    assert not any(item.label == 'Cancel interview' for item in hr_ui.button)
    assert any(f"Interview #{interview['id']}" in item.value and 'unread' in item.value for item in hr_ui.text)
    next(item for item in hr_ui.button if item.label == 'Mark cancellation read').click().run()
    assert not hr_ui.exception
    assert any(f"Interview #{interview['id']}" in item.value and '| read' in item.value for item in hr_ui.text)
    assert hr.request('GET', '/api/hr/interview-cancellations')['items'][0]['is_read']


def test_candidate_cancellation_refuses_confirmed_external_delivery(cancellation_case):
    path, _, _, candidate, user, _, interview = cancellation_case
    with connect(path) as db:
        row = db.execute('SELECT * FROM interviews WHERE id=?', (interview['id'],)).fetchone()
        db.execute("""INSERT INTO google_deliveries(interview_id,application_id,kind,recipient,start_at,end_at,state,created_by)
                      VALUES (?,?, 'email', 'synthetic@example.test',?,?, 'confirmed',?)""",
                   (row['id'], row['application_id'], row['start_at'], row['end_at'], row['created_by']))
    ui = ui_for(candidate, user)
    next(item for item in ui.checkbox if item.label == 'I want to cancel this interview').check()
    next(item for item in ui.button if item.label == 'Cancel interview').click().run()
    assert not ui.exception
    assert any('contact HR' in item.value for item in ui.error)
    assert candidate.request('GET', '/api/interviews')[0]['status'] == 'scheduled'
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM candidate_interview_cancellations').fetchone()[0] == 0
