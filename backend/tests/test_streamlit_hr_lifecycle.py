"""Exercise HR scheduling and offers through the real local API."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.db import connect
from app.main import create_app
from streamlit_client import LocalApi


@pytest.mark.parametrize("scenario", ["complete", "changed", "offer_changed", "hire_changed", "hire_expired", "uncertain", "expired", "withdraw_complete", "withdraw_booked", "withdraw_changed", "withdraw_unconfirmed", "withdraw_past"])
def test_hr_candidate_lifecycle_and_stale_actions(tmp_path, scenario):
    path = tmp_path / "hr-lifecycle.sqlite3"
    calls = []
    app = create_app(path, seed_demo=True)
    with TestClient(app) as server:
        def forward(request):
            if "/slots/batches/" in request.url.path and request.url.path.endswith("/commit"):
                calls.append(request.url.path)
                if scenario == "changed":
                    with connect(path) as db:
                        db.execute("UPDATE applications SET status='rejected' WHERE id=1")
                if scenario in ("uncertain", "expired"):
                    return httpx.Response(503 if scenario == "uncertain" else 401)
            if scenario == "offer_changed" and request.method == "POST" and request.url.path.endswith("/offers"):
                with connect(path) as db:
                    db.execute("UPDATE applications SET status='rejected' WHERE id=1")
            if request.method == "POST" and request.url.path.endswith("/withdraw"):
                calls.append(request.url.path)
                if scenario == "withdraw_booked":
                    slot_id = int(request.url.path.split("/")[-2])
                    candidate.request("POST", f"/api/slots/{slot_id}/book", json={"application_id": 1})
                if scenario == "withdraw_changed":
                    with connect(path) as db:
                        db.execute("UPDATE interview_slots SET target_application_id=NULL WHERE id=1")
            if request.method == "POST" and request.url.path.endswith("/decision"):
                calls.append(request.url.path)
                if scenario == "hire_changed":
                    with connect(path) as db:
                        db.execute("UPDATE job_offers SET status='withdrawn' WHERE application_id=1")
                if scenario == "hire_expired":
                    return httpx.Response(401)
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        candidate = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            user = hr.request("POST", "/api/auth/login", json={"username": "hr", "password": "hr-demo-only"})
            candidate_user = candidate.request("POST", "/api/auth/login", json={"username": "candidate", "password": "candidate-demo-only"})
            with connect(path) as db:
                application_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','Python SQL','advanced')", (candidate_user['id'],)).lastrowid
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=40)
            ui.session_state["local_api"] = hr
            ui.session_state["current_user"] = user
            ui.run()
            next(box for box in ui.selectbox if box.label == "Review job").set_value("job-backend-python").run()
            assert not ui.exception
            assert not any(field.label == "Offer details" for field in ui.text_area)
            start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
            end = start + timedelta(minutes=30)
            next(field for field in ui.text_input if field.label == "Slot start with UTC offset").set_value(start.isoformat())
            next(field for field in ui.text_input if field.label == "Slot end with UTC offset").set_value(end.isoformat())
            next(field for field in ui.text_input if field.label == "Interviewer").set_value("Synthetic interviewer")
            next(button for button in ui.button if button.label == "Preview private slot").click().run()
            assert not ui.exception
            assert any("Synthetic interviewer" in item.value for item in ui.text)
            next(button for button in ui.button if button.label == "Release private slot").click().run()
            assert not ui.exception
            assert len(calls) == 1
            with connect(path) as db:
                count = db.execute("SELECT count(*) FROM interview_slots").fetchone()[0]
            if scenario in ("changed", "uncertain", "expired"):
                assert count == 0
                if scenario == "changed":
                    assert any("changed" in item.value for item in ui.error)
                elif scenario == "uncertain":
                    assert any("could not be confirmed" in item.value for item in ui.warning)
                else:
                    assert "current_user" not in ui.session_state
                return
            assert count == 1
            if scenario.startswith("withdraw_"):
                assert any("unbooked" in item.value and "Slot #" in item.value for item in ui.text)
                if scenario == "withdraw_past":
                    with connect(path) as db:
                        old = datetime.now(UTC) - timedelta(hours=1)
                        db.execute("UPDATE interview_slots SET start_at=?,end_at=? WHERE id=1", (old.isoformat(), (old + timedelta(minutes=30)).isoformat()))
                    ui.run()
                    assert any("expired" in item.value and "Slot #" in item.value for item in ui.text)
                    assert not any(button.label == "Withdraw private slot" for button in ui.button)
                    return
                if scenario != "withdraw_unconfirmed":
                    next(box for box in ui.checkbox if box.label == "Confirm withdrawal of this private slot").check()
                next(button for button in ui.button if button.label == "Withdraw private slot").click().run()
                assert not ui.exception
                with connect(path) as db:
                    withdrawn = db.execute("SELECT withdrawn FROM interview_slots WHERE id=1").fetchone()[0]
                if scenario == "withdraw_complete":
                    assert withdrawn == 1 and any("withdrawn locally" in item.value for item in ui.success)
                elif scenario == "withdraw_unconfirmed":
                    assert withdrawn == 0 and len(calls) == 1 and any("Confirm the slot" in item.value for item in ui.error)
                else:
                    assert withdrawn == 0 and len(calls) == 2 and any("changed" in item.value for item in ui.error)
                return
            slots = candidate.request("GET", "/api/slots", params={"application_id": application_id})
            assert len(slots) == 1
            interview = candidate.request("POST", f"/api/slots/{slots[0]['id']}/book", json={"application_id": application_id})
            # Move this fictional appointment into the past to exercise explicit completion.
            past = datetime.now(UTC) - timedelta(hours=2)
            with connect(path) as db:
                db.execute("UPDATE interviews SET start_at=?,end_at=? WHERE id=?", (past.isoformat(), (past + timedelta(minutes=30)).isoformat(), interview['id']))
            ui.run()
            next(button for button in ui.button if button.label == "Confirm interview took place").click().run()
            assert not ui.exception
            assert any("Interview completion recorded" in item.value for item in ui.success)
            next(field for field in ui.text_area if field.label == "Offer details").set_value("Synthetic offer")
            next(button for button in ui.button if button.label == "Present offer locally").click().run()
            assert not ui.exception
            offers = candidate.request("GET", f"/api/applications/{application_id}/offers")
            if scenario == "offer_changed":
                assert not offers
                assert any("changed" in item.value for item in ui.error)
            else:
                assert any("Offer presented locally" in item.value for item in ui.success)
                assert len(offers) == 1 and offers[0]["details"] == "Synthetic offer" and offers[0]["status"] == "presented"
                candidate.request("POST", f"/api/offers/{offers[0]['id']}/respond", json={"decision": "accepted"})
                ui.run()
                assert any("accepted" in item.value for item in ui.text)
                next(button for button in ui.button if button.label == "Open masked review").click().run()
                next(radio for radio in ui.radio if radio.label == "Decision").set_value("hire")
                next(button for button in ui.button if button.label == "Record reviewed decision").click().run()
                assert calls[-1] == f"/api/applications/{application_id}/decision"
                with connect(path) as db:
                    final_status = db.execute("SELECT status FROM applications WHERE id=?", (application_id,)).fetchone()[0]
                if scenario == "hire_changed":
                    assert final_status == "offered"
                    assert any("expired or changed" in item.value for item in ui.info)
                elif scenario == "hire_expired":
                    assert final_status == "offered" and "current_user" not in ui.session_state
                else:
                    assert final_status == "hired"
                    assert any("Decision recorded" in item.value for item in ui.info)
        finally:
            hr.close()
            candidate.close()


def test_lifecycle_write_endpoints_reject_candidate(tmp_path):
    with TestClient(create_app(tmp_path / "roles.sqlite3", seed_demo=True)) as client:
        assert client.post("/api/auth/login", json={"username": "candidate", "password": "candidate-demo-only"}).status_code == 200
        start = datetime.now(UTC) + timedelta(days=1)
        item = {"application_id": 1, "start_at": start.isoformat(), "end_at": (start + timedelta(minutes=30)).isoformat(), "interviewer": "Synthetic"}
        assert client.post("/api/slots/batches/preview", json={"items": [item]}).status_code == 403
        assert client.post("/api/interviews/1/complete").status_code == 403
        assert client.post("/api/applications/1/offers", json={"details": "Synthetic"}).status_code == 403


def test_targeted_slots_filter_and_conditional_withdraw_recheck_binding(tmp_path):
    path = tmp_path / "slot-filter.sqlite3"
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post("/api/auth/login", json={"username": "hr", "password": "hr-demo-only"}).status_code == 200
        start = datetime.now(UTC) + timedelta(days=1)
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            ids = [db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','Python','advanced')", (candidate,)).lastrowid for _ in range(2)]
            slots = [db.execute("INSERT INTO interview_slots(start_at,end_at,interviewer,interviewer_key,created_by,target_application_id,target_candidate_id) VALUES (?,?,?,?,?,?,?)", (start.isoformat(), (start + timedelta(minutes=30)).isoformat(), 'Synthetic', 'synthetic', hr, app_id, candidate)).lastrowid for app_id in ids]
        assert len(client.get('/api/slots', params={'application_id': ids[0]}).json()) == 2
        filtered = client.get('/api/slots', params={'target_application_id': ids[0]}).json()
        assert [slot['id'] for slot in filtered] == [slots[0]]
        endpoint = f'/api/slots/{slots[0]}/withdraw'
        assert client.post(endpoint, params={'expected_application_id': ids[1]}).status_code == 409
        with connect(path) as db:
            db.execute("UPDATE interview_slots SET target_candidate_id=? WHERE id=?", (hr, slots[0]))
        assert client.post(endpoint, params={'expected_application_id': ids[0]}).status_code == 409
        with connect(path) as db:
            db.execute("UPDATE interview_slots SET target_candidate_id=? WHERE id=?", (candidate, slots[0]))
            db.execute("UPDATE applications SET candidate_id=? WHERE id=?", (hr, ids[0]))
        assert client.post(endpoint, params={'expected_application_id': ids[0]}).status_code == 409
        with connect(path) as db:
            db.execute("UPDATE applications SET candidate_id=?,status='rejected' WHERE id=?", (candidate, ids[0]))
        assert client.post(endpoint, params={'expected_application_id': ids[0]}).status_code == 409
        with connect(path) as db:
            db.execute("UPDATE applications SET status='advanced' WHERE id=?", (ids[0],))
        assert client.post(endpoint, params={'expected_application_id': ids[0]}).status_code == 200
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        assert client.get('/api/slots', params={'target_application_id': ids[1]}).status_code == 403
        assert client.post(f'/api/slots/{slots[1]}/withdraw', params={'expected_application_id': ids[1]}).status_code == 403
