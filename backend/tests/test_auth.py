from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.auth import User, get_current_user, require_owner_or_hr, require_roles
from app.db import connect
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(database_path=tmp_path / "test.sqlite3", seed_demo=True)

    hr_guard = require_roles("hr")

    @app.get("/test/hr")
    def hr_only(user: Annotated[User, Depends(hr_guard)]):
        return {"id": user.id}

    @app.get("/test/owner/{owner_id}")
    def owned(owner_id: int, user: Annotated[User, Depends(get_current_user)]):
        require_owner_or_hr(user, owner_id)
        return {"allowed": True}

    with TestClient(app) as client:
        yield client


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_health_and_authentication_lifecycle(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/auth/me").status_code == 401
    assert login(client, "candidate", "wrong").status_code == 401
    assert login(client, "candidate", "candidate-demo-only").status_code == 200
    assert client.get("/api/auth/me").json()["role"] == "candidate"
    cookie = client.cookies.get("tawzeef_session")
    assert cookie and len(cookie) >= 32
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("tawzeef_session", cookie)
    assert client.get("/api/auth/me").status_code == 401


def test_role_and_ownership_isolation(client):
    login(client, "candidate", "candidate-demo-only")
    candidate_id = client.get("/api/auth/me").json()["id"]
    assert client.get("/test/hr").status_code == 403
    assert client.get(f"/test/owner/{candidate_id}").status_code == 200
    assert client.get(f"/test/owner/{candidate_id + 1}").status_code == 403
    client.post("/api/auth/logout")
    login(client, "employee", "employee-demo-only")
    assert client.get(f"/test/owner/{candidate_id}").status_code == 403
    client.post("/api/auth/logout")
    login(client, "hr", "hr-demo-only")
    assert client.get("/test/hr").status_code == 200
    assert client.get(f"/test/owner/{candidate_id}").status_code == 200


def test_cross_origin_mutation_rejected_and_cookie_protected(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "hr", "password": "hr-demo-only"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    response = login(client, "hr", "hr-demo-only")
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()


def test_demo_accounts_are_opt_in(tmp_path):
    app = create_app(database_path=tmp_path / "empty.sqlite3", seed_demo=False)
    with TestClient(app) as client:
        assert login(client, "hr", "hr-demo-only").status_code == 401


def test_existing_demo_credentials_cannot_be_reused_when_demo_disabled(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with TestClient(create_app(database_path=path, seed_demo=True)) as client:
        assert login(client, "hr", "hr-demo-only").status_code == 200
    with pytest.raises(RuntimeError, match="Demo credentials exist"), TestClient(
        create_app(database_path=path, seed_demo=False)
    ):
        pass


def test_expired_session_is_rejected_and_database_stores_no_raw_token(client):
    login(client, "hr", "hr-demo-only")
    token = client.cookies.get("tawzeef_session")
    with connect(client.app.state.database_path) as db:
        stored = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert stored != token
        db.execute("UPDATE sessions SET expires_at = 0")
    assert client.get("/api/auth/me").status_code == 401
