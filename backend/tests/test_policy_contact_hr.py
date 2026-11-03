"""Unsupported policy questions have a local, non-sending HR next step."""

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app

NEXT_STEP = "Contact HR for help with this policy question."


@pytest.mark.parametrize("role", ["candidate", "employee", "hr"])
def test_policy_abstention_next_step_is_visible_and_persists(tmp_path, role):
    app = create_app(tmp_path / "policy-next-step.sqlite3", seed_demo=True)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": role, "password": f"{role}-demo-only"}).status_code == 200
        conversation = client.post("/api/conversations", json={"title": "Fictional policy"}).json()["id"]
        endpoint = f"/api/conversations/{conversation}/messages"
        result = client.post(endpoint, json={"question": "What is the xenobright nebular policy?", "mode": "policy"})
        assert result.status_code == 201
        assert result.json()["mode"] == "abstain" and result.json()["next_step"] == NEXT_STEP
        restored = client.get(f"/api/conversations/{conversation}").json()["messages"][-1]
        assert restored["mode"] == "abstain" and restored["next_step"] == NEXT_STEP
        assert restored["sources"] == []
        with connect(app.state.database_path) as db:
            stored = db.execute("SELECT answer FROM conversation_messages WHERE id=?", (restored["id"],)).fetchone()[0]
            assert NEXT_STEP not in stored  # The display-time next step also works for older saved abstentions.
        # General questions do not acquire a policy-specific escalation.
        general = client.post(endpoint, json={"question": "What is the xenobright nebular policy?", "mode": "general"})
        assert general.status_code == 201 and "next_step" not in general.json()


def test_graph_abstention_next_step_is_scoped_and_not_checkpointed(tmp_path):
    path = tmp_path / "graph-next-step.sqlite3"
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post("/api/auth/login", json={"username": "hr", "password": "hr-demo-only"}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?, 'job-backend-python', 'fictional.pdf', 'Synthetic')", (candidate,)).lastrowid
        created = client.post("/api/graph-reviews", json={"application_id": application, "intent": "policy", "policy_question": "What is the xenobright nebular policy?"})
        assert created.status_code == 201
        result = created.json()
        assert result["state"]["policy"] == {"status": "abstain", "next_step": NEXT_STEP}
        restored = client.get(f"/api/graph-reviews/{result['id']}")
        assert restored.json()["state"]["policy"]["next_step"] == NEXT_STEP
        with connect(path) as db:
            for row in db.execute("SELECT checkpoint FROM checkpoints"):
                assert NEXT_STEP.encode() not in bytes(row[0])
        status = client.post("/api/graph-reviews", json={"application_id": application, "intent": "status"})
        assert status.status_code == 201 and "policy" not in status.json()["state"]
