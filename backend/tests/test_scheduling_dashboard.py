import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.dashboard import router as dashboard_router
from app.db import connect
from app.main import create_app
from app.scheduling import initialize_scheduling
from app.scheduling import router as scheduling_router


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "test.sqlite3"
    app = create_app(database_path=path, seed_demo=True)
    app.include_router(scheduling_router)
    app.include_router(dashboard_router)
    with TestClient(app) as client:
        initialize_scheduling(path)
        yield client


def login(client, role):
    response = client.post("/api/auth/login", json={"username": role, "password": f"{role}-demo-only"})
    assert response.status_code == 200
    return response.json()


def create_application(client):
    document = Document()
    document.add_paragraph("Python SQL REST API")
    stream = BytesIO()
    document.save(stream)
    response = client.post(
        "/api/applications",
        params={"job_id": "job-backend-python", "filename": "resume.docx"},
        content=stream.getvalue(),
    )
    assert response.status_code == 201
    return response.json()["id"]


def appointment(app_id, start="2030-01-02T11:00:00+02:00", end="2030-01-02T12:00:00+02:00", interviewer="Mona"):
    return {"application_id": app_id, "start_at": start, "end_at": end, "interviewer": interviewer}


def test_local_calendar_normalizes_utc_checks_conflicts_and_audits(client):
    login(client, "candidate")
    first = create_application(client)
    second = create_application(client)
    login(client, "hr")
    response = client.post("/api/interviews", json=appointment(first))
    assert response.status_code == 201
    assert response.json()["start_at"] == "2030-01-02T09:00:00.000000Z"
    assert response.json()["end_at"] == "2030-01-02T10:00:00.000000Z"
    # Same candidate conflicts across applications, regardless of interviewer.
    assert client.post("/api/interviews", json=appointment(second, interviewer="Ali")).status_code == 409
    # Adjacent time slots are allowed.
    assert client.post("/api/interviews", json=appointment(second, "2030-01-02T12:00:00+02:00", "2030-01-02T13:00:00+02:00")).status_code == 201
    assert sum(event["action"] == "interview_scheduled" for event in client.get("/api/audit").json()) == 2
    assert client.post("/api/interviews", json=appointment(first, "2030-01-02T09:00:00", "2030-01-02T10:00:00Z")).status_code == 422
    assert client.post("/api/interviews", json=appointment(first, "2030-01-02T11:00:00Z", "2030-01-02T10:00:00Z")).status_code == 422


def test_candidate_and_interviewer_conflicts_and_ownership(client):
    login(client, "candidate")
    first = create_application(client)
    with connect(client.app.state.database_path) as db:
        new_id = db.execute("INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                            ("other", "Other", "candidate", hash_password("other-password"))).lastrowid
        second = db.execute("INSERT INTO applications (candidate_id, job_id, filename, resume_text) VALUES (?, ?, ?, ?)",
                            (new_id, "job-backend-python", "other.pdf", "Python")).lastrowid
    login(client, "hr")
    assert client.post("/api/interviews", json=appointment(first)).status_code == 201
    assert client.post("/api/interviews", json=appointment(second, interviewer="  MONA  ")).status_code == 409
    assert client.post("/api/interviews", json=appointment(second, interviewer="Ali")).status_code == 201
    assert len(client.get("/api/interviews").json()) == 2
    login(client, "candidate")
    own = client.get("/api/interviews")
    assert own.status_code == 200 and len(own.json()) == 1 and own.json()[0]["application_id"] == first
    assert client.post("/api/interviews", json=appointment(first)).status_code == 403
    assert client.get("/api/dashboard").status_code == 403
    login(client, "employee")
    assert client.get("/api/interviews").status_code == 403
    assert client.get("/api/dashboard").status_code == 403


def test_concurrent_booking_cannot_double_book_same_candidate(client):
    login(client, "candidate")
    app_id = create_application(client)
    login(client, "hr")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda name: client.post("/api/interviews", json=appointment(app_id, interviewer=name)).status_code, ["Nadia", "Amr"]))
    assert sorted(results) == [201, 409]
    assert len(client.get("/api/interviews").json()) == 1


def test_dashboard_zero_and_known_hire_duration(client, accepted_offer):
    login(client, "hr")
    empty = client.get("/api/dashboard").json()
    assert empty["applications_total"] == 0 and empty["screening_events"] == 0
    assert empty["pipeline"] == [] and empty["time_to_hire"]["average_days"] is None
    login(client, "candidate")
    app_id = create_application(client)
    login(client, "hr")
    assert client.post(f"/api/applications/{app_id}/screen").status_code == 200
    assert client.post(f"/api/applications/{app_id}/screen").status_code == 200
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "advance"}).status_code == 200
    accepted_offer(client, app_id)
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "hire"}).status_code == 200
    with connect(client.app.state.database_path) as db:
        db.execute("UPDATE applications SET created_at = '2030-01-01T00:00:00Z' WHERE id = ?", (app_id,))
        db.execute("UPDATE audit_events SET created_at = '2030-01-04T12:00:00Z' WHERE application_id = ? AND action = 'decision' AND details LIKE '%hire%'", (app_id,))
    result = client.get("/api/dashboard").json()
    assert result["applications_total"] == 1
    assert result["screening_events"] == 2
    assert result["pipeline"] == [{"status": "hired", "count": 1}]
    assert result["time_to_hire"]["count"] == 1
    assert result["time_to_hire"]["average_days"] == 3.5


def test_past_and_finalized_interviews_are_rejected(client):
    login(client, "candidate")
    app_id = create_application(client)
    login(client, "hr")
    past = datetime.now(UTC) - timedelta(days=1)
    assert client.post("/api/interviews", json=appointment(app_id, past.isoformat(), (past + timedelta(hours=1)).isoformat())).status_code == 422
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "reject"}).status_code == 200
    future = datetime.now(UTC) + timedelta(days=1)
    assert client.post("/api/interviews", json=appointment(app_id, future.isoformat(), (future + timedelta(hours=1)).isoformat())).status_code == 409
    assert client.get("/api/interviews").json() == []


def test_concurrent_rejection_and_booking_observe_serialized_state(client):
    login(client, "candidate")
    app_id = create_application(client)
    login(client, "hr")
    future = datetime.now(UTC) + timedelta(days=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        booking = executor.submit(client.post, "/api/interviews", json=appointment(app_id, future.isoformat(), (future + timedelta(hours=1)).isoformat()))
        decision = executor.submit(client.post, f"/api/applications/{app_id}/decision", json={"decision": "reject"})
        booked, rejected = booking.result().status_code, decision.result().status_code
    assert rejected == 200
    assert booked in (201, 409)
    interviews = client.get("/api/interviews").json()
    assert len(interviews) == (1 if booked == 201 else 0)
    events = client.get("/api/audit").json()
    relevant = [event["action"] for event in reversed(events) if event["action"] in {"interview_scheduled", "decision"}]
    assert relevant == (["interview_scheduled", "decision"] if booked == 201 else ["decision"])


def test_booking_and_audit_rollback_together(client):
    login(client, "candidate")
    app_id = create_application(client)
    login(client, "hr")
    with connect(client.app.state.database_path) as db:
        db.execute("CREATE TRIGGER abort_booking_audit BEFORE INSERT ON audit_events WHEN NEW.action = 'interview_scheduled' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    future = datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        client.post("/api/interviews", json=appointment(app_id, future.isoformat(), (future + timedelta(hours=1)).isoformat()))
    assert client.get("/api/interviews").json() == []
    assert not any(event["action"] == "interview_scheduled" for event in client.get("/api/audit").json())


def test_hr_can_cancel_future_interview_and_slot_becomes_available(client):
    login(client, "candidate")
    first = create_application(client)
    second = create_application(client)
    login(client, "hr")
    future = datetime.now(UTC) + timedelta(days=1)
    item = appointment(first, future.isoformat(), (future + timedelta(hours=1)).isoformat())
    created = client.post("/api/interviews", json=item).json()
    assert created["status"] == "scheduled"
    login(client, "candidate")
    assert client.post(f"/api/interviews/{created['id']}/cancel").status_code == 403
    login(client, "hr")
    cancelled = client.post(f"/api/interviews/{created['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/interviews/{created['id']}/cancel").status_code == 409
    assert client.post("/api/interviews", json={**item, "application_id": second}).status_code == 201
    assert any(event["action"] == "interview_cancelled" and 'hr_cancelled' in event["details"] for event in client.get("/api/audit").json())
    login(client, "candidate")
    assert {interview["status"] for interview in client.get("/api/interviews").json()} == {"scheduled", "cancelled"}


def test_rejection_cancels_only_future_interviews_and_audits_atomically(client):
    login(client, "candidate")
    app_id = create_application(client)
    login(client, "hr")
    future = datetime.now(UTC) + timedelta(days=1)
    upcoming = client.post("/api/interviews", json=appointment(app_id, future.isoformat(), (future + timedelta(hours=1)).isoformat())).json()
    with connect(client.app.state.database_path) as db:
        db.execute("INSERT INTO interviews (application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by) SELECT id,candidate_id,'2020-01-01T09:00:00.000000Z','2020-01-01T10:00:00.000000Z','Past','past',candidate_id FROM applications WHERE id=?", (app_id,))
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "reject"}).status_code == 200
    interviews = client.get("/api/interviews").json()
    assert [i["status"] for i in interviews] == ["scheduled", "cancelled"]
    audit = client.get("/api/audit").json()
    assert any(e["action"] == "interview_cancelled" and 'application_rejected' in e["details"] and str(upcoming["id"]) in e["details"] for e in audit)


def test_cancellation_audit_failure_rolls_back_rejection_and_interview(client):
    login(client, "candidate")
    app_id = create_application(client)
    login(client, "hr")
    future = datetime.now(UTC) + timedelta(days=1)
    client.post("/api/interviews", json=appointment(app_id, future.isoformat(), (future + timedelta(hours=1)).isoformat()))
    with connect(client.app.state.database_path) as db:
        db.execute("CREATE TRIGGER abort_cancellation BEFORE INSERT ON audit_events WHEN NEW.action = 'interview_cancelled' BEGIN SELECT RAISE(ABORT, 'cancellation audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="cancellation audit failure"):
        client.post(f"/api/applications/{app_id}/decision", json={"decision": "reject"})
    assert client.get(f"/api/applications/{app_id}").json()["status"] == "submitted"
    assert client.get("/api/interviews").json()[0]["status"] == "scheduled"
    assert not any(event["action"] == "decision" for event in client.get("/api/audit").json())


def test_schema_migration_preserves_old_interviews(tmp_path):
    path = tmp_path / "old.sqlite3"
    with connect(path) as db:
        db.execute("CREATE TABLE interviews (id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, interviewer TEXT NOT NULL, interviewer_key TEXT NOT NULL, created_by INTEGER NOT NULL, created_at TEXT NOT NULL)")
        db.execute("INSERT INTO interviews VALUES (1,1,1,'2030-01-01T00:00:00.000000Z','2030-01-01T01:00:00.000000Z','Mona','mona',1,'2029-01-01T00:00:00Z')")
    initialize_scheduling(path)
    initialize_scheduling(path)
    with connect(path) as db:
        row = db.execute("SELECT * FROM interviews WHERE id = 1").fetchone()
        assert row["status"] == "scheduled" and row["interviewer"] == "Mona"
