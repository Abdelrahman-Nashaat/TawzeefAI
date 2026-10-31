"""Local-only API entry point. Do not expose demo credentials on a network."""

import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.admin_users import initialize_admin_users
from app.admin_users import router as admin_users_router
from app.agent import AgentError, run_agent
from app.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    User,
    create_session,
    get_current_user,
    hash_password,
    token_digest,
    verify_password,
)
from app.conversations import _authorized_sources, initialize_conversations
from app.conversations import router as conversations_router
from app.dashboard import router as dashboard_router
from app.db import connect, initialize
from app.evaluation_dashboard import router as evaluation_router
from app.google_outbox import initialize_google_outbox
from app.google_outbox import router as google_outbox_router
from app.graph_orchestrator import router as graph_reviews_router
from app.hybrid_api import router as hybrid_router
from app.interview_calendar import router as interview_calendar_router
from app.jobs_admin import initialize_jobs_admin
from app.jobs_admin import router as jobs_admin_router
from app.knowledge import answer_question
from app.knowledge_documents import initialize_knowledge_documents
from app.knowledge_documents import router as knowledge_documents_router
from app.recruitment import router as recruitment_router
from app.recruitment import seed_jobs
from app.resume_fields_api import router as resume_fields_router
from app.rubric_admin import initialize_rubric_admin
from app.rubric_admin import router as rubric_admin_router
from app.rubric_comparison import router as rubric_comparison_router
from app.scheduling import initialize_scheduling
from app.scheduling import router as scheduling_router
from app.slots import initialize_slots
from app.slots import router as slots_router
from app.workflow import initialize_workflows
from app.workflow import router as workflow_router


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class KnowledgeQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


DEMO_ACCOUNTS = (
    ("candidate", "Demo Candidate", "candidate", "candidate-demo-only"),
    ("employee", "Demo Employee", "employee", "employee-demo-only"),
    ("hr", "Demo HR", "hr", "hr-demo-only"),
)


def create_app(database_path: str | Path | None = None, seed_demo: bool = False) -> FastAPI:
    path = Path(database_path or os.environ.get("TAWZEEF_DATABASE", "tawzeefai.sqlite3"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        path.parent.mkdir(parents=True, exist_ok=True)
        initialize(path)
        initialize_admin_users(path)
        initialize_scheduling(path)
        initialize_google_outbox(path)
        initialize_slots(path)
        initialize_workflows(path)
        initialize_knowledge_documents(path, include_samples=seed_demo)
        initialize_jobs_admin(path)
        initialize_rubric_admin(path)
        initialize_conversations(path)
        if not seed_demo:
            with connect(path) as db:
                for username, _, _, password in DEMO_ACCOUNTS:
                    row = db.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
                    if row is not None and verify_password(password, row["password_hash"]):
                        raise RuntimeError("Demo credentials exist in this database; run explicitly in demo mode or use another database.")
        if seed_demo:
            seed_jobs(path)
            with connect(path) as db:
                for username, display_name, role, password in DEMO_ACCOUNTS:
                    if not db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                        db.execute(
                            "INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                            (username, display_name, role, hash_password(password)),
                        )
        yield

    application = FastAPI(title="TawzeefAI Local API", lifespan=lifespan)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )
    application.state.database_path = path
    if os.environ.get('TAWZEEF_ENABLE_GOOGLE') == '1':
        if sys.platform != 'win32':
            raise RuntimeError('Google local integration requires Windows Credential Manager')
        from app.google_dispatch import prepare_calendar_removal, prepare_dispatch

        application.state.google_preflight = prepare_dispatch
        application.state.google_calendar_removal_preflight = prepare_calendar_removal
    application.include_router(recruitment_router)
    application.include_router(resume_fields_router)
    application.include_router(scheduling_router)
    application.include_router(slots_router)
    application.include_router(dashboard_router)
    application.include_router(workflow_router)
    application.include_router(knowledge_documents_router)
    application.include_router(jobs_admin_router)
    application.include_router(rubric_admin_router)
    application.include_router(rubric_comparison_router)
    application.include_router(conversations_router)
    application.include_router(graph_reviews_router)
    application.include_router(evaluation_router)
    application.include_router(admin_users_router)
    application.include_router(interview_calendar_router)
    application.include_router(hybrid_router)
    application.include_router(google_outbox_router)

    @application.middleware("http")
    async def check_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.headers.get("host")):
                    return JSONResponse({"detail": "Cross-origin request denied"}, status_code=403)
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return JSONResponse({"detail": "Cross-origin request denied"}, status_code=403)
        return await call_next(request)

    @application.get("/api/health")
    def health():
        return {"status": "ok", "mode": "local-development"}

    @application.post("/api/auth/login")
    def login(payload: Login, request: Request, response: Response):
        with connect(path) as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (payload.username,)).fetchone()
        if row is None or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        old = request.cookies.get(COOKIE_NAME)
        if old:
            with connect(path) as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(old),))
        token = create_session(request, row["id"], expected_password_hash=row["password_hash"])
        response.set_cookie(
            COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/"
        )
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "role": row["role"]}

    @application.post("/api/auth/logout", status_code=204)
    def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            with connect(path) as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(token),))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        return response

    @application.get("/api/auth/me")
    def me(user: Annotated[User, Depends(get_current_user)]):
        return asdict(user)

    @application.post("/api/knowledge/answer")
    def knowledge_answer(payload: KnowledgeQuestion, request: Request, user: Annotated[User, Depends(get_current_user)]):
        def authorize_sources(sources):
            with connect(path) as db:
                db.execute('BEGIN')
                token = request.cookies.get(COOKIE_NAME, '')
                actor = db.execute('''SELECT u.id, u.username, u.role FROM sessions s
                    JOIN users u ON u.id=s.user_id
                    WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',
                    (token_digest(token), int(time.time()))).fetchone()
                if actor is None or actor['id'] != user.id or actor['username'] != user.username:
                    raise HTTPException(status_code=401, detail='Authentication required')
                if actor['role'] != user.role:
                    raise HTTPException(status_code=409, detail='Access changed; please retry')
                available = _authorized_sources(db, actor['role'])
                if any(available.get(source.id) != source for source in sources):
                    raise HTTPException(status_code=409, detail='Source access changed; please retry')

        answer = answer_question(
            payload.question, user.role, database_path=path,
            enable_groq=os.environ.get("TAWZEEF_ENABLE_GROQ") == "1",
            authorize_sources=authorize_sources,
        )
        authorize_sources(answer.sources)
        return asdict(answer)

    @application.post("/api/agent/{application_id}/review")
    def agent_review(application_id: int, request: Request, user: Annotated[User, Depends(get_current_user)]):
        def authorize():
            current = get_current_user(request)
            if current.id != user.id or current.role != 'hr':
                raise HTTPException(status_code=403, detail='HR access required')

        try:
            return run_agent(
                path, application_id, user, 'prepare review',
                enabled=os.environ.get('TAWZEEF_ENABLE_GROQ') == '1',
                authorize=authorize,
            )
        except AgentError as exc:
            code = {'forbidden': 403, 'invalid_application': 404}.get(exc.code, 400)
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    frontend = Path(__file__).resolve().parents[2] / "frontend"
    if frontend.is_dir():
        @application.get("/", include_in_schema=False)
        def index():
            return FileResponse(frontend / "index.html")

        application.mount("/assets", StaticFiles(directory=frontend), name="frontend")

    return application


app = create_app(seed_demo=os.environ.get("TAWZEEF_DEMO", "0") == "1")
