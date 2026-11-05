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


def test_candidate_sees_and_acknowledges_cancellation_in_streamlit(tmp_path):
    app = create_app(tmp_path / 'candidate-ui.sqlite3', seed_demo=True)
    with TestClient(app) as server:
        def forward(request):
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
        candidate = LocalApi(8000, transport=httpx.MockTransport(forward))
        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            user = candidate.request('POST', '/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'})
            hr.request('POST', '/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
            with connect(app.state.database_path) as db:
                app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.docx','sample','advanced')", (user['id'],)).lastrowid
            start = datetime.now(UTC) + timedelta(days=1)
            meeting = hr.request('POST', '/api/interviews', json={'application_id': app_id, 'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'HR'})
            hr.request('POST', f"/api/interviews/{meeting['id']}/cancel")
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=120)
            ui.session_state['local_api'] = candidate
            ui.session_state['current_user'] = user
            ui.run()
            assert not ui.exception
            assert any(f"Interview #{meeting['id']}" in item.value and 'unread' in item.value for item in ui.text)
            next(button for button in ui.button if button.label == 'Mark cancellation notice read').click().run()
            assert not ui.exception
            assert candidate.request('GET', '/api/candidate/interview-cancellations')['items'][0]['is_read']
            assert any(f"Interview #{meeting['id']}" in item.value and 'read' in item.value for item in ui.text)
        finally:
            candidate.close()
            hr.close()
