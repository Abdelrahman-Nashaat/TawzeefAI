"""Local screening and recruitment endpoints. All résumé content is untrusted."""

import json
import re
import unicodedata
from hashlib import sha256
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import User, get_current_user, require_owner_or_hr, require_roles
from app.blind import redact_resume
from app.db import connect
from app.document_text import MAX_FILE_BYTES, DocumentTextError, extract_document_text
from app.google_outbox import cancellation_block_reason
from app.resume_structure import structure_resume
from app.scheduling import utc_text

router = APIRouter(prefix="/api")
Candidate = Annotated[User, Depends(require_roles("candidate"))]
HR = Annotated[User, Depends(require_roles("hr"))]
Authenticated = Annotated[User, Depends(get_current_user)]
FIXTURE_FILE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "jobs.json"


def seed_jobs(path: Path) -> None:
    jobs = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))["jobs"]
    with connect(path) as db:
        for job in jobs:
            db.execute(
                """INSERT OR IGNORE INTO jobs
                   (id, title, location, required_skills, preferred_skills, responsibilities)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    job["id"], job["title"], job["location"],
                    json.dumps(job["required_skills"], ensure_ascii=False),
                    json.dumps(job["preferred_skills"], ensure_ascii=False),
                    json.dumps(job["responsibilities"], ensure_ascii=False),
                ),
            )


def job_data(row) -> dict:
    job = dict(row)
    for field in ("required_skills", "preferred_skills", "responsibilities"):
        job[field] = json.loads(job[field])
    return job


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def contains(text: str, phrase: str) -> bool:
    # Latin token boundaries allow Arabic conjunctions such as وHTML while
    # still preventing SQL matching the separate word NoSQL.
    boundary = r"[a-z0-9_]" if phrase.isascii() else r"\w"
    return re.search(rf"(?<!{boundary})" + re.escape(phrase) + rf"(?!{boundary})", text) is not None


ALIASES = {
    "http apis": ["http api", "http apis", "rest api", "rest apis", "backend api", "backend apis"],
    "etl pipelines": ["etl", "etl pipeline", "etl pipelines"],
    "html and css": ["html", "css"],
    "automated testing": ["automated testing", "automated tests", "unit tests"],
}


# Conservative heuristics: skip an entire sentence with an explicit negative
# or instruction-like phrase, since interpreting scope reliably needs a model
# and human review. These patterns cannot detect every phrasing or language.
UNSAFE_CONTEXT = re.compile(
    r"\b(?:no|not|never|without|lack(?:s|ing|ed)?|none|neither|cannot|can't|unable)\b"
    r"|\b(?:ignore|disregard|override|reveal|bypass)\b"
    r"|\b(?:system prompt|previous instructions|assign (?:a )?(?:perfect|highest) score)\b"
    r"|(?:لا|لم|ليس|بدون|تنقصني|تجاهل|اكشف)(?=\s|$)",
    re.IGNORECASE,
)
WORK_CONTEXT = re.compile(
    r"\b(?:built|developed|implemented|maintained|used|created|designed|deployed|"
    r"worked|wrote|managed|integrated|tested|built|experience)\b"
    r"|(?:اشتغلت|عملت|طورت|نفذت|استخدمت|بنيت|صممت|خبرة)",
    re.IGNORECASE,
)


def score_resume(job: dict, text: str) -> dict:
    blind = redact_resume(text)
    segments = [normalize(part) for part in re.split(r"[\n.!?;؛]+", blind.text) if part.strip()]
    usable = [segment for segment in segments if not UNSAFE_CONTEXT.search(segment)]

    def assess(skill: str) -> dict:
        key = normalize(skill)
        alternatives = ALIASES.get(key, [key])
        # Both HTML and CSS must be present for the combined requirement.
        present = lambda term: any(contains(segment, term) for segment in usable)
        found = all(present(term) for term in alternatives) if key == "html and css" else any(
            present(term) for term in alternatives
        )
        evidence = [segment[:240] for segment in usable if any(contains(segment, term) for term in alternatives)] if found else []
        return {"skill": skill, "mentioned": found, "evidence": evidence[:2]}

    required = [assess(skill) for skill in job["required_skills"]]
    preferred = [assess(skill) for skill in job["preferred_skills"]]
    matched = sum(item["mentioned"] for item in required)
    return {
        "score": round(100 * matched / len(required)) if required else 0,
        "required": required,
        "preferred": preferred,
        "evidence_status": "limited" if matched == len(required) and any(WORK_CONTEXT.search(s) for s in usable) else "insufficient",
        "blind_screening": {"requires_review": blind.requires_review, "notice": blind.notice, "masked": blind.masked},
        "method": "Conservative keyword mentions; negated or instruction-like sentences are excluded heuristically. Not verified experience or an automated hiring decision; human review required.",
    }


def screening_audit_details(score: dict, serialized: str) -> str:
    """Record screening metadata without duplicating résumé excerpts in audit.

    The digest identifies the exact serialized result stored on the application;
    it does not retain previous results after the application is screened again.
    """
    required = score['required']
    preferred = score['preferred']
    return json.dumps({
        'method': 'keyword_mentions_v1',
        'score': score['score'],
        'evidence_status': score['evidence_status'],
        'required_mentioned': sum(item['mentioned'] for item in required),
        'required_total': len(required),
        'preferred_mentioned': sum(item['mentioned'] for item in preferred),
        'preferred_total': len(preferred),
        'result_sha256': sha256(serialized.encode('utf-8')).hexdigest(),
    }, ensure_ascii=False)


def application_view(row, user: User) -> dict:
    result = {key: row[key] for key in ("id", "candidate_id", "job_id", "status", "created_at", "updated_at")}
    if user.role == "hr":
        result.update({"filename": row["filename"], "resume_text": row["resume_text"],
                       "score": json.loads(row["score_json"]) if row["score_json"] else None})
    return result


def get_application(db, application_id: int):
    row = db.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return row


@router.get("/jobs")
def list_jobs(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        return [job_data(row) for row in db.execute("SELECT * FROM jobs ORDER BY title")]


@router.post("/applications", status_code=201)
async def apply(request: Request, user: Candidate, job_id: str, filename: str):
    if len(filename) > 255 or not filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Drop path information even when a Windows path is supplied.
    filename = PurePath(filename.replace("\\", "/")).name
    with connect(request.app.state.database_path) as db:
        if db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Job not found")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds 5 MiB limit")
        chunks.extend(chunk)
    try:
        text = extract_document_text(filename, bytes(chunks))
    except DocumentTextError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    with connect(request.app.state.database_path) as db:
        cursor = db.execute(
            "INSERT INTO applications (candidate_id, job_id, filename, resume_text) VALUES (?, ?, ?, ?)",
            (user.id, job_id, filename, text),
        )
        application_id = cursor.lastrowid
        db.execute("INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)",
                   (application_id, user.id, "submitted", "{}"))
        return application_view(get_application(db, application_id), user)


@router.get("/applications")
def list_applications(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        if user.role == "hr":
            rows = db.execute("SELECT * FROM applications ORDER BY id DESC").fetchall()
        elif user.role == "candidate":
            rows = db.execute("SELECT * FROM applications WHERE candidate_id = ? ORDER BY id DESC", (user.id,)).fetchall()
        else:
            raise HTTPException(status_code=403, detail="Access denied")
        return [application_view(row, user) for row in rows]


@router.get("/applications/{application_id}")
def application_detail(application_id: int, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        row = get_application(db, application_id)
        require_owner_or_hr(user, row["candidate_id"])
        return application_view(row, user)


@router.get("/applications/{application_id}/resume/structured")
def structured_resume(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        row = get_application(db, application_id)
        source = redact_resume(row['resume_text']).text
    return structure_resume(source)


@router.post("/applications/{application_id}/screen")
def screen(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        row = get_application(db, application_id)
        job = job_data(db.execute("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)).fetchone())
        score = score_resume(job, row["resume_text"])
        serialized = json.dumps(score, ensure_ascii=False)
        db.execute("UPDATE applications SET score_json = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (serialized, application_id))
        db.execute("INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)", (application_id, user.id, "screened", screening_audit_details(score, serialized)))
        return score


class Decision(BaseModel):
    decision: Literal["advance", "reject", "hire"]
    note: str = Field(default="", max_length=1000)


@router.post("/applications/{application_id}/decision")
def decide(application_id: int, payload: Decision, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        application = get_application(db, application_id)
        permitted = {
            "submitted": {"advance", "reject"},
            "advanced": {"hire", "reject"},
            "hired": set(),
            "rejected": set(),
        }
        if payload.decision not in permitted.get(application["status"], set()):
            raise HTTPException(status_code=409, detail="Decision is not allowed from current status")
        state = {"advance": "advanced", "reject": "rejected", "hire": "hired"}[payload.decision]
        db.execute("UPDATE applications SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (state, application_id))
        details = json.dumps({"decision": payload.decision, "note": payload.note}, ensure_ascii=False)
        db.execute("INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)", (application_id, user.id, "decision", details))
        if payload.decision == "reject":
            now = utc_text(datetime.now(UTC))
            upcoming = db.execute(
                "SELECT id FROM interviews WHERE application_id = ? AND status = 'scheduled' AND start_at > ?",
                (application_id, now),
            ).fetchall()
            for interview in upcoming:
                if cancellation_block_reason(db, interview['id']):
                    raise HTTPException(status_code=409, detail='Resolve the Google delivery before rejecting this application')
                db.execute("UPDATE interviews SET status = 'cancelled' WHERE id = ?", (interview["id"],))
                db.execute(
                    "INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)",
                    (application_id, user.id, "interview_cancelled", json.dumps({"interview_id": interview["id"], "reason": "application_rejected"})),
                )
        return application_view(get_application(db, application_id), user)


@router.get("/audit")
def audit(request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        return [dict(row) for row in db.execute("SELECT * FROM audit_events ORDER BY id DESC")]
