"""The optional UI shows the policy next step from the real API."""

from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.main import create_app
from streamlit_client import LocalApi


def test_streamlit_displays_policy_next_step_from_saved_conversation(tmp_path):
    app = create_app(tmp_path / "policy-ui.sqlite3", seed_demo=True)
    with TestClient(app) as server:
        def forward(request):
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
        client = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            user = client.request("POST", "/api/auth/login", json={"username": "employee", "password": "employee-demo-only"})
            conversation = client.request("POST", "/api/conversations", json={"title": "Fictional policy"})
            client.request("POST", f"/api/conversations/{conversation['id']}/messages", json={"mode": "policy", "question": "What is the xenobright nebular policy?"})
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=30)
            ui.session_state["local_api"] = client
            ui.session_state["current_user"] = user
            ui.run()
            assert not ui.exception
            assert any(item.value == "Contact HR for help with this policy question." for item in ui.info)
        finally:
            client.close()
