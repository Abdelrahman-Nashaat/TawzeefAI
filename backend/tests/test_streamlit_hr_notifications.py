from io import BytesIO
from pathlib import Path

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient

pytest.importorskip('streamlit')
from streamlit.testing.v1 import AppTest

from app.main import create_app
from streamlit_client import LocalApi


def test_hr_can_acknowledge_submission_in_streamlit(tmp_path):
    with TestClient(create_app(tmp_path / 'inbox.sqlite3', seed_demo=True)) as server:
        def forward(request):
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        candidate = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            candidate.request('POST', '/api/auth/login', json={'username':'candidate','password':'candidate-demo-only'})
            doc = Document()
            doc.add_paragraph('Synthetic resume')
            out = BytesIO()
            doc.save(out)
            candidate.request('POST', '/api/applications', params={'job_id':'job-backend-python','filename':'synthetic.docx'}, content=out.getvalue())
            user = hr.request('POST', '/api/auth/login', json={'username':'hr','password':'hr-demo-only'})
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=40)
            ui.session_state['local_api'] = hr
            ui.session_state['current_user'] = user
            ui.run()
            assert not ui.exception
            assert any('unread' in item.value and 'Application #1' in item.value for item in ui.text)
            next(button for button in ui.button if button.label == 'Mark read').click().run()
            assert not ui.exception
            assert any('| read' in item.value and 'Application #1' in item.value for item in ui.text)
            assert hr.request('GET', '/api/hr/notifications')['items'][0]['is_read']
        finally:
            hr.close()
            candidate.close()
