"""Local screening and recruitment endpoints. All résumé content is untrusted."""

import json
import re
import time
import unicodedata
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.auth import (
    COOKIE_NAME,
    User,
    get_current_user,
    require_owner_or_hr,
    require_roles,
    token_digest,
)
from app.blind import BlindText, redact_resume
from app.db import connect
from app.document_text import (
    MAX_FILE_BYTES,
    OCR_NOTICE,
    DocumentTextError,
    extract_document_text,
    extract_pdf_pages_with_ocr,
    require_ocr_enabled,
)
from app.google_outbox import cancellation_block_reason
from app.resume_mask import masked_source
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
    # Latin token boundaries allow an Arabic conjunction attached to HTML while
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
    r"\b(?:no|not|never|without|lack(?:s|ing|ed)?|none|neither|cannot|unable)\b"
    r"|\b(?:ca|do|does|did|is|are|was|were|have|has|had|wo|would|should|could|must|need)n['’]t\b"
    r"|\b(?:zero\s+(?:prior\s+)?experience|unfamiliar|yet\s+to\s+learn|seeking\s+to\s+learn)\b"
    r"|\b(?:ignore|disregard|override|reveal|bypass)\b"
    r"|\b(?:system prompt|previous instructions|assign (?:a )?(?:perfect|highest) score)\b"
    r"|(?:لا|لم|ليس|لست|غير|بدون|تنقصني|تجاهل|اكشف)(?=\s|$)"
    r"|(?:عديم(?:ة)?\s+الخبرة|أتمنى\s+تعلم|اتمنى\s+تعلم)",
    re.IGNORECASE,
)
WORK_CONTEXT = re.compile(
    r"\b(?:built|developed|implemented|maintained|used|created|designed|deployed|"
    r"worked|wrote|managed|integrated|tested|built|experience)\b"
    r"|(?:اشتغلت|عملت|طورت|نفذت|استخدمت|بنيت|صممت|خبرة)",
    re.IGNORECASE,
)


def score_resume(job: dict, text: str) -> dict:
    return _score_blind(job, redact_resume(text))


def score_masked_resume(job: dict, source: dict) -> dict:
    """Score the selected source without applying redaction a second time."""
    blind = BlindText(text=source['text'], masked=source['automatic_masked'] or source['additional_masked'])
    result = _score_blind(job, blind)
    if source['source_revision'] or source['mask_revision']:
        result['blind_screening']['review_status'] = source['status']
    return result


def _score_blind(job: dict, blind: BlindText) -> dict:
    # Keep semicolon-separated lists together so a preceding negation applies
    # conservatively to every item. A dot inside Node.js or .NET is not a
    # sentence boundary; split dots only when followed by whitespace or EOF.
    segments = [normalize(part) for part in re.split(r"[\n!?]+|\.+(?=\s|$)", blind.text) if part.strip()]
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

    The digest identifies the exact serialized result stored on the application
    and, for new screenings, the restricted historical snapshot.
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


def store_screening(db, application_id: int, actor_id: int, score: dict) -> None:
    """Atomically store the current score, audit event, and historical result."""
    serialized = json.dumps(score, ensure_ascii=False)
    db.execute("UPDATE applications SET score_json = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (serialized, application_id))
    event = db.execute("INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)",
                       (application_id, actor_id, 'screened', screening_audit_details(score, serialized)))
    db.execute('INSERT INTO screening_history(audit_event_id,score_json) VALUES (?,?)',
               (event.lastrowid, serialized))


def application_view(row, user: User) -> dict:
    result = {key: row[key] for key in ("id", "candidate_id", "job_id", "status", "created_at", "updated_at")}
    result['extraction'] = json.loads(row['extraction_json']) if row['extraction_json'] else None
    if user.role == "hr":
        result.update({"filename": row["filename"], "resume_text": row["resume_text"],
                       "score": json.loads(row["score_json"]) if row["score_json"] else None})
    return result


def get_application(db, application_id: int):
    row = db.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return row


def _require_current_role(db, request: Request, user: User, role: str) -> None:
    """Recheck the authenticated actor inside the write transaction."""
    actor = db.execute('''SELECT u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.user_id=? AND s.token_hash=? AND s.expires_at>? AND u.active=1''',
        (user.id, token_digest(request.cookies.get(COOKIE_NAME, '')), int(time.time()))).fetchone()
    if actor is None or actor['username'] != user.username:
        raise HTTPException(status_code=401, detail='Authentication required')
    if actor['role'] != role:
        raise HTTPException(status_code=403, detail=f'{role.capitalize()} access required')


@router.get("/jobs")
def list_jobs(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        return [job_data(row) for row in db.execute("SELECT * FROM jobs ORDER BY title")]


@router.post("/applications", status_code=201)
async def apply(request: Request, user: Candidate, job_id: str, filename: str,
                ocr: bool = False, ocr_language: Literal['eng', 'eng+ara'] = 'eng'):
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
        extraction = None
        if ocr:
            require_ocr_enabled()
            pages, ocr_pages = await run_in_threadpool(extract_pdf_pages_with_ocr, filename, bytes(chunks), ocr_language)
            text = '\n'.join(page['text'] for page in pages if page['text'])
            extraction = {'ocr_pages': ocr_pages, 'notice': OCR_NOTICE}
        else:
            text = await run_in_threadpool(extract_document_text, filename, bytes(chunks))
    except DocumentTextError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'candidate')
        if db.execute('SELECT 1 FROM jobs WHERE id = ?', (job_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Job not found')
        cursor = db.execute(
            "INSERT INTO applications (candidate_id, job_id, filename, resume_text, extraction_json) VALUES (?, ?, ?, ?, ?)",
            (user.id, job_id, filename, text, json.dumps(extraction) if extraction is not None else None),
        )
        application_id = cursor.lastrowid
        db.execute("INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)",
                   (application_id, user.id, "submitted", "{}"))
        from app.provisional_queue import create_pending_snapshot

        create_pending_snapshot(db, application_id, job_id, user.id)
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
        db.execute('BEGIN')
        get_application(db, application_id)
        source = masked_source(db, application_id)['text']
    return structure_resume(source)


@router.get("/applications/{application_id}/duplicates")
def duplicate_resume_candidates(application_id: int, request: Request, user: HR):
    """Identify exact normalized text matches for human review, not fraud.

    Only this application's complete extracted text is compared. The response
    does not expose résumé text, filenames, candidate identities or hashes.
    """
    limit = 2000
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        row = get_application(db, application_id)
        target = normalize(row['resume_text'])
        matches = []
        scanned = 0
        if target:
            rows = db.execute('''SELECT id,resume_text FROM applications
                WHERE id<>? ORDER BY id DESC LIMIT ?''', (application_id, limit + 1))
            for other in rows:
                if scanned == limit:
                    break
                scanned += 1
                if normalize(other['resume_text']) == target:
                    matches.append({'application_id': other['id']})
            truncated = db.execute('SELECT count(*) FROM applications WHERE id<>?',
                                   (application_id,)).fetchone()[0] > limit
        else:
            truncated = False
        return {'matches': matches, 'scanned': scanned, 'truncated': truncated,
                'notice': 'Exact normalized extracted text only. Similarity does not prove fraud; HR must review.'}


@router.post("/applications/{application_id}/screen")
def screen(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        row = get_application(db, application_id)
        job = job_data(db.execute("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)).fetchone())
        score = score_masked_resume(job, masked_source(db, application_id))
        store_screening(db, application_id, user.id, score)
        _require_current_role(db, request, user, 'hr')
        return score


@router.get('/applications/{application_id}/screening-history')
def screening_history(application_id: int, request: Request, user: HR,
                      limit: int = Query(default=20, ge=1, le=100),
                      before: int | None = Query(default=None, ge=1)):
    """Return historical results only to HR; snapshots may contain résumé excerpts."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        get_application(db, application_id)
        rows = db.execute('''SELECT a.id,a.actor_id,a.created_at,a.details,h.score_json
            FROM audit_events a LEFT JOIN screening_history h ON h.audit_event_id=a.id
            WHERE a.application_id=? AND a.action='screened' AND (? IS NULL OR a.id<?)
            ORDER BY a.id DESC LIMIT ?''', (application_id, before, before, limit + 1)).fetchall()
        entries = []
        for row in rows[:limit]:
            status, result = 'legacy_unavailable', None
            if row['score_json'] is not None:
                status = 'integrity_error'
                try:
                    expected = json.loads(row['details'])['result_sha256']
                    decoded = json.loads(row['score_json'])
                    if (isinstance(decoded, dict) and isinstance(expected, str) and
                            sha256(row['score_json'].encode('utf-8')).hexdigest() == expected):
                        status, result = 'available', decoded
                except (TypeError, ValueError, KeyError):
                    pass
            entries.append({'audit_event_id': row['id'], 'actor_id': row['actor_id'],
                            'created_at': row['created_at'], 'status': status, 'result': result})
        _require_current_role(db, request, user, 'hr')
        return {'entries': entries, 'next_before': rows[limit - 1]['id'] if len(rows) > limit else None,
                'notice': 'Historical keyword screening results may contain partially redacted résumé excerpts. '
                          'They are not verified qualifications or hiring decisions.'}


class OfferCreate(BaseModel):
    details: str = Field(min_length=1, max_length=4000)


class OfferReply(BaseModel):
    decision: Literal['accepted', 'declined']


def _offer_audit(db, application_id: int, actor_id: int, action: str, offer_id: int) -> None:
    db.execute('INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,?,?)',
               (application_id, actor_id, action, json.dumps({'offer_id': offer_id})))


def _upcoming_interview(db, application_id: int) -> bool:
    return bool(db.execute("""SELECT 1 FROM interviews WHERE application_id=? AND status='scheduled'
        AND end_at>? LIMIT 1""", (application_id, utc_text(datetime.now(UTC)))).fetchone())


def _offer_view(row) -> dict:
    return {key: row[key] for key in ('id', 'application_id', 'details', 'status', 'created_at', 'responded_at')}


@router.get('/applications/{application_id}/offers')
def list_offers(application_id: int, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, user.role)
        application = get_application(db, application_id)
        require_owner_or_hr(user, application['candidate_id'])
        return [_offer_view(row) for row in db.execute(
            'SELECT * FROM job_offers WHERE application_id=? ORDER BY id DESC', (application_id,))]


@router.post('/applications/{application_id}/offers', status_code=201)
def create_offer(application_id: int, payload: OfferCreate, request: Request, user: HR):
    details = payload.details.strip()
    if not details:
        raise HTTPException(status_code=422, detail='Offer details are required')
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        application = get_application(db, application_id)
        if application['status'] != 'advanced':
            raise HTTPException(status_code=409, detail='Application is not ready for an offer')
        if not db.execute('''SELECT 1 FROM interviews i JOIN interview_completions c ON c.interview_id=i.id
                             WHERE i.application_id=? LIMIT 1''', (application_id,)).fetchone():
            raise HTTPException(status_code=409, detail='A completed interview is required')
        if _upcoming_interview(db, application_id):
            raise HTTPException(status_code=409, detail='Resolve active interviews before presenting an offer')
        cursor = db.execute("INSERT INTO job_offers(application_id,details,status,created_by) VALUES (?,?,'presented',?)",
                            (application_id, details, user.id))
        db.execute("UPDATE applications SET status='offered',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (application_id,))
        _offer_audit(db, application_id, user.id, 'offer_presented', cursor.lastrowid)
        _require_current_role(db, request, user, 'hr')
        return _offer_view(db.execute('SELECT * FROM job_offers WHERE id=?', (cursor.lastrowid,)).fetchone())


@router.post('/offers/{offer_id}/respond')
def respond_offer(offer_id: int, payload: OfferReply, request: Request, user: Candidate):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'candidate')
        offer = db.execute('''SELECT o.*,a.candidate_id,a.status AS application_status FROM job_offers o
                              JOIN applications a ON a.id=o.application_id WHERE o.id=? AND a.candidate_id=?''',
                           (offer_id, user.id)).fetchone()
        if not offer:
            raise HTTPException(status_code=404, detail='Offer not found')
        if offer['status'] != 'presented' or offer['application_status'] != 'offered':
            raise HTTPException(status_code=409, detail='Offer is no longer awaiting a response')
        db.execute("UPDATE job_offers SET status=?,responded_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (payload.decision, offer_id))
        if payload.decision == 'declined':
            db.execute("UPDATE applications SET status='advanced',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (offer['application_id'],))
        _offer_audit(db, offer['application_id'], user.id, 'offer_' + payload.decision, offer_id)
        _require_current_role(db, request, user, 'candidate')
        return _offer_view(db.execute('SELECT * FROM job_offers WHERE id=?', (offer_id,)).fetchone())


@router.post('/offers/{offer_id}/withdraw')
def withdraw_offer(offer_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        offer = db.execute('''SELECT o.*,a.status AS application_status FROM job_offers o JOIN applications a
                              ON a.id=o.application_id WHERE o.id=?''', (offer_id,)).fetchone()
        if not offer:
            raise HTTPException(status_code=404, detail='Offer not found')
        if offer['status'] != 'presented' or offer['application_status'] != 'offered':
            raise HTTPException(status_code=409, detail='Only pending offers can be withdrawn')
        db.execute("UPDATE job_offers SET status='withdrawn',responded_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (offer_id,))
        db.execute("UPDATE applications SET status='advanced',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (offer['application_id'],))
        _offer_audit(db, offer['application_id'], user.id, 'offer_withdrawn', offer_id)
        _require_current_role(db, request, user, 'hr')
        return _offer_view(db.execute('SELECT * FROM job_offers WHERE id=?', (offer_id,)).fetchone())


class Decision(BaseModel):
    decision: Literal["advance", "reject", "hire"]
    note: str = Field(default="", max_length=1000)


@router.post("/applications/{application_id}/decision")
def decide(application_id: int, payload: Decision, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_current_role(db, request, user, 'hr')
        application = get_application(db, application_id)
        permitted = {
            "submitted": {"advance", "reject"},
            "advanced": {"reject"},
            "offered": {"hire", "reject"},
            "hired": set(),
            "rejected": set(),
        }
        if payload.decision not in permitted.get(application["status"], set()):
            raise HTTPException(status_code=409, detail="Decision is not allowed from current status")
        if payload.decision == 'hire':
            if not db.execute("SELECT 1 FROM job_offers WHERE application_id=? AND status='accepted'", (application_id,)).fetchone():
                raise HTTPException(status_code=409, detail='Candidate must accept an offer before hiring')
            if _upcoming_interview(db, application_id):
                raise HTTPException(status_code=409, detail='Resolve active interviews before hiring')
        if payload.decision == 'reject':
            offered = db.execute("SELECT id FROM job_offers WHERE application_id=? AND status='presented'", (application_id,)).fetchone()
            if offered:
                db.execute("UPDATE job_offers SET status='withdrawn', responded_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (offered['id'],))
                _offer_audit(db, application_id, user.id, 'offer_withdrawn', offered['id'])
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
        if payload.decision in ('hire', 'reject'):
            # Private availability for a finalized application cannot be booked.
            # Release only future, unbooked (or already cancelled) windows;
            # keep general slots and scheduled interviews under their existing
            # cancellation/Google reconciliation rules.
            now = utc_text(datetime.now(UTC))
            private_slots = db.execute("""SELECT s.id FROM interview_slots s
                LEFT JOIN interviews i ON i.id=s.interview_id
                WHERE s.target_application_id=? AND s.withdrawn=0 AND s.start_at>?
                  AND (s.interview_id IS NULL OR i.status='cancelled')""",
                (application_id, now)).fetchall()
            for slot in private_slots:
                db.execute('UPDATE interview_slots SET withdrawn=1 WHERE id=?', (slot['id'],))
                db.execute('INSERT INTO slot_audit_events(slot_id,actor_id,action) VALUES (?,?,?)',
                           (slot['id'], user.id, 'slot_withdrawn'))
        _require_current_role(db, request, user, 'hr')
        return application_view(get_application(db, application_id), user)


@router.get("/audit")
def audit(request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        return [dict(row) for row in db.execute("SELECT * FROM audit_events ORDER BY id DESC")]
