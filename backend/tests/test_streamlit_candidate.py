"""Exercise the optional candidate UI against the real local API."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.main import create_app
from streamlit_candidate import submit_application
from streamlit_client import ApiError, ApiUnavailable, LocalApi


def test_candidate_can_view_and_book_an_interview_through_streamlit(tmp_path):
    app = create_app(tmp_path / "candidate-ui.sqlite3", seed_demo=True)
    with TestClient(app) as server:
        failures = {"candidate-screening": None, "candidate-assessment": None}
        screening_calls = []

        def forward(request):
            for endpoint, status in failures.items():
                if request.url.path.endswith("/" + endpoint):
                    if endpoint == "candidate-screening":
                        screening_calls.append(request.url.path)
                    if status:
                        return httpx.Response(status, json={"detail": "unavailable"})
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        candidate = LocalApi(8000, transport=httpx.MockTransport(forward))
        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            user = candidate.request("POST", "/api/auth/login", json={"username": "candidate", "password": "candidate-demo-only"})
            hr.request("POST", "/api/auth/login", json={"username": "hr", "password": "hr-demo-only"})
            job = candidate.request("GET", "/api/jobs")[0]
            document = Document()
            document.add_paragraph("Synthetic candidate built Python APIs and worked on automated testing.")
            payload = BytesIO()
            document.save(payload)
            application = submit_application(candidate, job["id"], "synthetic.docx", payload.getvalue(), False, "eng")
            app_id = application["id"]
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=30)
            ui.session_state["local_api"] = candidate
            ui.session_state["current_user"] = user
            ui.run()
            assert not ui.exception
            assert not screening_calls
            assert any("Applied" in value.value for value in ui.text)
            assert any("No current assessment has been shared" in value.value for value in ui.info)
            assert any("No interviews have been recorded" in value.value for value in ui.info)

            next(button for button in ui.button if button.label == "Load current keyword preview").click().run()
            assert not ui.exception
            assert any("Matching text:" in item.value and "python" in item.value for item in ui.text)
            assert any("not detected" in item.value for item in ui.text)
            assert any("not verified ability" in item.value for item in ui.caption)
            ui.run()
            assert not any("Matching text:" in item.value for item in ui.text)

            mask = hr.request("GET", f"/api/applications/{app_id}/resume/mask")
            start = mask["source"].index("Python")
            hr.request("PUT", f"/api/applications/{app_id}/resume/mask", json={
                "source_sha256": mask["source_sha256"], "expected_source_revision": mask["source_revision"],
                "expected_revision": mask["revision"], "spans": [{"start": start, "end": start + len("Python")}],
            })
            next(button for button in ui.button if button.label == "Load current keyword preview").click().run()
            assert any("Python: not detected" in item.value for item in ui.text)
            assert not any("Matching text:" in item.value and "python" in item.value for item in ui.text)
            ui.run()
            assert not any("Matching text:" in item.value for item in ui.text)

            hr.request("POST", f"/api/applications/{app_id}/decision", json={"decision": "advance"})
            start = datetime.now(UTC) + timedelta(days=2)
            slot = hr.request("POST", "/api/slots", json={"start_at": start.isoformat(),
                "end_at": (start + timedelta(minutes=30)).isoformat(), "interviewer": "Synthetic interviewer"})

            ui.run()
            assert not ui.exception
            assert any("No current assessment has been shared" in value.value for value in ui.info)
            assert any("Shortlisted" in value.value for value in ui.text)
            assert any("Synthetic interviewer" in value for value in ui.selectbox[-1].options)
            next(button for button in ui.button if button.label == "Book selected interview").click().run()
            assert not ui.exception
            assert any("Interview booked" in value.value for value in ui.success)
            interviews = candidate.request("GET", "/api/interviews")
            assert len(interviews) == 1
            assert interviews[0]["application_id"] == app_id
            assert candidate.request("GET", "/api/slots", params={"application_id": app_id}) == []
            with pytest.raises(ApiError) as error:
                candidate.request("POST", f"/api/slots/{slot['id']}/book", json={"application_id": app_id})
            assert error.value.status == 409

            for status in (409, 503):
                failures["candidate-screening"] = status
                next(button for button in ui.button if button.label == "Load current keyword preview").click().run()
                assert any("panel is unavailable" in value.value for value in ui.warning)
                assert any("No current assessment" in value.value for value in ui.info)
                assert any("Knowledge conversation" in value.value for value in ui.header)
                assert any("Synthetic interviewer" in value.value for value in ui.text)
            failures["candidate-screening"] = None
            for status in (409, 503):
                failures["candidate-assessment"] = status
                ui.run()
                assert any("panel is unavailable" in value.value for value in ui.warning)
                assert any("Knowledge conversation" in value.value for value in ui.header)
                assert any("Synthetic interviewer" in value.value for value in ui.text)
            failures["candidate-assessment"] = None
            failures["candidate-screening"] = 401
            next(button for button in ui.button if button.label == "Load current keyword preview").click().run()
            assert not ui.exception
            assert any("session is unavailable" in value.value for value in ui.error)
            assert not any("Knowledge conversation" in value.value for value in ui.header)
        finally:
            candidate.close()
            hr.close()


@pytest.mark.parametrize("error", [ApiUnavailable("unavailable"), ApiError(500)])
def test_uncertain_upload_is_not_retried(monkeypatch, error):
    messages = []
    calls = []

    class UnavailableApi:
        def request(self, method, path, **kwargs):
            calls.append((method, path))
            raise error

    monkeypatch.setattr("streamlit_candidate.st.warning", messages.append)
    assert submit_application(UnavailableApi(), "job", "resume.docx", b"synthetic", False, "eng") is None
    assert calls == [("POST", "/api/applications")]
    assert "before trying again" in messages[0]


@pytest.mark.parametrize("outcome", ["accepted", "declined", "withdrawn", "uncertain", "expired"])
def test_candidate_offer_response_uses_owner_api_and_handles_conflicts(tmp_path, outcome):
    from app.db import connect

    database = tmp_path / "offer-ui.sqlite3"
    with TestClient(create_app(database, seed_demo=True)) as server:
        calls = []
        hr = None

        def forward(request):
            nonlocal hr
            path = request.url.path
            if path.endswith("/respond"):
                calls.append(path)
                if outcome == "withdrawn":
                    hr.request("POST", path.replace("/respond", "/withdraw"))
                if outcome == "uncertain":
                    return httpx.Response(503, json={"detail": "unavailable"})
                if outcome == "expired":
                    return httpx.Response(401, json={"detail": "expired"})
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        candidate = LocalApi(8000, transport=httpx.MockTransport(forward))
        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            user = candidate.request("POST", "/api/auth/login", json={"username": "candidate", "password": "candidate-demo-only"})
            hr_user = hr.request("POST", "/api/auth/login", json={"username": "hr", "password": "hr-demo-only"})
            with connect(database) as db:
                app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','fictional.docx','Python','offered')", (user['id'],)).lastrowid
                offer_id = db.execute("INSERT INTO job_offers(application_id,recipient_id,details,status,created_by) VALUES (?,?,?,'presented',?)", (app_id, user['id'], 'Fictional offer details', hr_user['id'])).lastrowid
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=30)
            ui.session_state["local_api"] = candidate
            ui.session_state["current_user"] = user
            ui.run()
            assert not ui.exception
            assert any('Fictional offer details' in item.value for item in ui.text)
            next(radio for radio in ui.radio if radio.label == 'Your response').set_value('Decline' if outcome == 'declined' else 'Accept')
            next(button for button in ui.button if button.label == 'Submit offer response').click().run()
            assert not ui.exception
            assert calls == [f'/api/offers/{offer_id}/respond']
            if outcome in ('accepted', 'declined'):
                assert any('response was recorded' in message.value for message in ui.success)
                assert candidate.request('GET', f'/api/applications/{app_id}/offers')[0]['status'] == outcome
                assert not any(button.label == 'Submit offer response' for button in ui.button)
            elif outcome == 'withdrawn':
                assert any('no longer awaiting' in message.value for message in ui.error)
                ui.run()
                assert any('withdrawn' in item.value for item in ui.text)
                assert not any(button.label == 'Submit offer response' for button in ui.button)
            elif outcome == 'uncertain':
                assert any('could not be confirmed' in message.value for message in ui.warning)
            else:
                assert any('Sign in again' in message.value for message in ui.error)
                assert 'current_user' not in ui.session_state
                assert not any('Knowledge conversation' in item.value for item in ui.header)
        finally:
            candidate.close()
            hr.close()
