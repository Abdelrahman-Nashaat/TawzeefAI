"""Local screening and recruitment endpoints. All résumé content is untrusted."""

import json
import re
import time
import unicodedata
from bisect import bisect_right
from datetime import UTC, datetime
from hashlib import sha256
from itertools import islice
from pathlib import Path, PurePath
from threading import BoundedSemaphore
from typing import Annotated, Literal
from uuid import uuid4

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
from app.resume_structure import extract_skill_candidates, structure_resume
from app.scheduling import utc_text

router = APIRouter(prefix="/api")
Candidate = Annotated[User, Depends(require_roles("candidate"))]
HR = Annotated[User, Depends(require_roles("hr"))]
Authenticated = Annotated[User, Depends(get_current_user)]
FIXTURE_FILE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "jobs.json"
_SCREEN_WORKERS = BoundedSemaphore(2)
MAX_DIRECT_SCREEN_CHARS = 200_000
MAX_DIRECT_SCREEN_WORK = 25_000_000
MAX_DIRECT_JOB_JSON_CHARS = 21_000


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

    # Normalization collapses all whitespace, so a newline cannot occur in a
    # segment or term. It is therefore a safe non-word segment separator for
    # the same boundary patterns used by contains().
    joined = "\n".join(usable)
    starts = []
    position = 0
    for segment in usable:
        starts.append(position)
        position += len(segment) + 1
    matches_by_term = {}

    def matching_segments(term: str) -> tuple[int, ...]:
        if term in matches_by_term:
            return matches_by_term[term]
        if not term:
            # Preserve the legacy zero-width behavior on each actual segment;
            # separators must not create additional matches for an empty term.
            result = tuple(islice((index for index, segment in enumerate(usable) if contains(segment, term)), 2))
        else:
            boundary = r"[a-z0-9_]" if term.isascii() else r"\w"
            pattern = re.compile(rf"(?<!{boundary})" + re.escape(term) + rf"(?!{boundary})")
            found_indices = []
            offset = 0
            while len(found_indices) < 2:
                match = pattern.search(joined, offset)
                if match is None:
                    break
                index = bisect_right(starts, match.start()) - 1
                found_indices.append(index)
                if index + 1 >= len(starts):
                    break
                offset = starts[index + 1]
            result = tuple(found_indices)
        matches_by_term[term] = result
        return result

    def assess(skill: str) -> dict:
        key = normalize(skill)
        alternatives = ALIASES.get(key, [key])
        matches = [matching_segments(term) for term in alternatives]
        # Both HTML and CSS must be present for the combined requirement.
        found = all(matches) if key == "html and css" else any(matches)
        indices = sorted({index for group in matches for index in group})[:2] if found else []
        return {"skill": skill, "mentioned": found, "evidence": [usable[index][:240] for index in indices]}

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
        db.execute("INSERT INTO hr_notifications(application_id,submitted_job_id) VALUES (?,?)", (application_id, job_id))
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
        _require_current_role(db, request, user, 'hr')
        get_application(db, application_id)
        blind = masked_source(db, application_id)
    result = structure_resume(blind['text'])
    result['skill_candidates'] = extract_skill_candidates(blind['text'], result)
    result['source_context'] = blind['context_sha256']
    result['source_revision'] = blind['source_revision']
    result['mask_revision'] = blind['mask_revision']
    result['mask_review_status'] = blind['status']
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        get_application(db, application_id)
        if masked_source(db, application_id)['context_sha256'] != blind['context_sha256']:
            raise HTTPException(status_code=409, detail='Resume or mask changed; reload the extracted fields')
    return result


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


def _screen_input_revision(db, application_id: int) -> tuple[int, int, int]:
    """Version source, job, status and recorded screenings without requiring graph setup."""
    revision = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                          (application_id,)).fetchone()
    status = db.execute('SELECT revision FROM hr_review_status_revisions WHERE application_id=?',
                        (application_id,)).fetchone()
    screening = db.execute("SELECT COALESCE(MAX(id),0) FROM audit_events WHERE application_id=? AND action='screened'",
                           (application_id,)).fetchone()[0]
    return revision['revision'] if revision else 0, status['revision'] if status else 0, screening


def _direct_screen_job(db, job_id: str) -> dict:
    row = db.execute('SELECT required_skills,preferred_skills FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail='Job requirements unavailable')
    if any(len(row[field]) > MAX_DIRECT_JOB_JSON_CHARS for field in ('required_skills', 'preferred_skills')):
        raise HTTPException(status_code=422, detail='Job requirements exceed the screening work limit; review manually')
    try:
        job = {field: json.loads(row[field]) for field in ('required_skills', 'preferred_skills')}
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail='Job requirements are invalid; review manually') from None
    if (any(not isinstance(value, list) or len(value) > 50 for value in job.values()) or
            any(not isinstance(skill, str) or len(skill) > 200 for values in job.values() for skill in values)):
        raise HTTPException(status_code=422, detail='Job requirements exceed the screening work limit; review manually')
    return job


@router.post("/applications/{application_id}/screen")
def screen(application_id: int, request: Request, user: HR):
    if not _SCREEN_WORKERS.acquire(blocking=False):
        raise HTTPException(status_code=503, detail='Screening is busy; retry after checking the application')
    try:
        with connect(request.app.state.database_path) as db:
            db.execute('BEGIN')
            _require_current_role(db, request, user, 'hr')
            row = get_application(db, application_id)
            if len(row['resume_text']) > MAX_DIRECT_SCREEN_CHARS:
                raise HTTPException(status_code=422, detail='Resume exceeds the screening work limit; review manually')
            job_id, owner, status = row['job_id'], row['candidate_id'], row['status']
            job = _direct_screen_job(db, job_id)
            source = masked_source(db, application_id)
            revisions = _screen_input_revision(db, application_id)
        # Bound whole normalized source times distinct search alternatives. Masking
        # may expand a source, so raw file length alone is not a safe work bound.
        terms = {term for skill in job['required_skills'] + job['preferred_skills']
                 for term in ALIASES.get(normalize(skill), [normalize(skill)])}
        if len(normalize(source['text'])) * max(1, len(terms)) > MAX_DIRECT_SCREEN_WORK:
            raise HTTPException(status_code=422, detail='Screening work limit exceeded; review manually')
        score = score_masked_resume(job, source)
        with connect(request.app.state.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            _require_current_role(db, request, user, 'hr')
            current = get_application(db, application_id)
            if (current['job_id'] != job_id or current['candidate_id'] != owner or current['status'] != status or
                    _screen_input_revision(db, application_id) != revisions):
                raise HTTPException(status_code=409, detail='Screening inputs changed; reload and screen again')
            store_screening(db, application_id, user.id, score)
            _require_current_role(db, request, user, 'hr')
            return score
    finally:
        _SCREEN_WORKERS.release()


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
    return {key: row[key] for key in ('id', 'application_id', 'recipient_id', 'details', 'status', 'created_at', 'responded_at')}


@router.get('/applications/{application_id}/offers')
def list_offers(application_id: int, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, user.role)
        application = get_application(db, application_id)
        require_owner_or_hr(user, application['candidate_id'])
        if user.role == 'hr':
            rows = db.execute('SELECT * FROM job_offers WHERE application_id=? ORDER BY id DESC', (application_id,))
        else:
            rows = db.execute('SELECT * FROM job_offers WHERE application_id=? AND recipient_id=? ORDER BY id DESC',
                              (application_id, user.id))
        return [_offer_view(row) for row in rows]


def validate_offer_eligibility(db, application_id: int):
    """Recheck offer eligibility in the caller's transaction."""
    application = get_application(db, application_id)
    if application['status'] != 'advanced':
        raise HTTPException(status_code=409, detail='Application is not ready for an offer')
    if not db.execute("SELECT 1 FROM users WHERE id=? AND role='candidate' AND active=1",
                      (application['candidate_id'],)).fetchone():
        raise HTTPException(status_code=409, detail='An active candidate account is required')
    if not db.execute('''SELECT 1 FROM interviews i JOIN interview_completions c ON c.interview_id=i.id
                         WHERE i.application_id=? AND i.candidate_id=? LIMIT 1''',
                      (application_id, application['candidate_id'])).fetchone():
        raise HTTPException(status_code=409, detail='A completed interview is required')
    if _upcoming_interview(db, application_id):
        raise HTTPException(status_code=409, detail='Resolve active interviews before presenting an offer')
    return application


def record_offer(db, application_id: int, details: str, actor_id: int):
    """Present one recipient-bound local offer in the caller's transaction."""
    application = validate_offer_eligibility(db, application_id)
    cursor = db.execute("INSERT INTO job_offers(application_id,recipient_id,details,status,created_by) VALUES (?,?,?,'presented',?)",
                        (application_id, application['candidate_id'], details, actor_id))
    db.execute("UPDATE applications SET status='offered',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (application_id,))
    _offer_audit(db, application_id, actor_id, 'offer_presented', cursor.lastrowid)
    return _offer_view(db.execute('SELECT * FROM job_offers WHERE id=?', (cursor.lastrowid,)).fetchone())


@router.post('/applications/{application_id}/offers', status_code=201)
def create_offer(application_id: int, payload: OfferCreate, request: Request, user: HR):
    details = payload.details.strip()
    if not details:
        raise HTTPException(status_code=422, detail='Offer details are required')
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        offer = record_offer(db, application_id, details, user.id)
        _require_current_role(db, request, user, 'hr')
        return offer


@router.post('/offers/{offer_id}/respond')
def respond_offer(offer_id: int, payload: OfferReply, request: Request, user: Candidate):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'candidate')
        offer = db.execute('''SELECT o.*,a.status AS application_status FROM job_offers o
                              JOIN applications a ON a.id=o.application_id
                              WHERE o.id=? AND a.candidate_id=? AND o.recipient_id=?''',
                           (offer_id, user.id, user.id)).fetchone()
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


def initialize_hr_reviews(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS hr_review_status_revisions (
                application_id INTEGER PRIMARY KEY REFERENCES applications(id), revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TRIGGER IF NOT EXISTS hr_review_status_changed AFTER UPDATE OF status ON applications
            WHEN OLD.status IS NOT NEW.status BEGIN
                INSERT OR IGNORE INTO hr_review_status_revisions(application_id,revision) VALUES(NEW.id,0);
                UPDATE hr_review_status_revisions SET revision=revision+1 WHERE application_id=NEW.id;
            END;
            CREATE TABLE IF NOT EXISTS hr_review_references (
                reference TEXT PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
                actor_id INTEGER NOT NULL REFERENCES users(id), session_hash TEXT NOT NULL,
                job_id TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
                status_revision INTEGER NOT NULL, expires_at INTEGER NOT NULL, UNIQUE(actor_id, application_id)
            );
            CREATE INDEX IF NOT EXISTS hr_review_references_expiry ON hr_review_references(expires_at);
        ''')


def _review_revision(db, application_id: int) -> int:
    row = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                     (application_id,)).fetchone()
    return row['revision'] if row else 0


def _review_status_revision(db, application_id: int) -> int:
    row = db.execute('SELECT revision FROM hr_review_status_revisions WHERE application_id=?',
                     (application_id,)).fetchone()
    return row['revision'] if row else 0


@router.post('/applications/{application_id}/hr-review-preview')
def hr_review_preview(application_id: int, request: Request, user: HR):
    """Capture visible masked evidence with an actor/session-bound review reference."""
    now = int(time.time())
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        application = get_application(db, application_id)
        if len(application['resume_text']) > 4000:
            raise HTTPException(status_code=422, detail='Resume exceeds the review preview limit; use the main interface.')
        source = masked_source(db, application_id)
        job_row = db.execute('SELECT * FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
        if job_row is None:
            raise HTTPException(status_code=409, detail='Job requirements unavailable')
        job = job_data(job_row)
        revision = _review_revision(db, application_id)
        db.execute('DELETE FROM hr_review_references WHERE expires_at<=?', (now,))
        db.execute('DELETE FROM hr_review_references WHERE actor_id=? AND application_id=?', (user.id, application_id))
        if db.execute('SELECT count(*) FROM hr_review_references').fetchone()[0] >= 2000:
            raise HTTPException(status_code=503, detail='Review capacity reached; try again later.')
        reference = uuid4().hex
        db.execute('''INSERT INTO hr_review_references
            (reference,application_id,actor_id,session_hash,job_id,status,revision,status_revision,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (reference, application_id, user.id, token_digest(request.cookies.get(COOKIE_NAME, '')),
             application['job_id'], application['status'], revision, _review_status_revision(db, application_id), now + 600))
        _require_current_role(db, request, user, 'hr')
        return {'reference': reference, 'application_id': application_id, 'job_id': application['job_id'],
                'job': job, 'status': application['status'], 'source': source['text'], 'source_status': source['status'],
                'notice': source['notice'], 'expires_in_seconds': 600}


class Decision(BaseModel):
    decision: Literal["advance", "reject", "hire"]
    note: str = Field(default="", max_length=1000)
    review_reference: str | None = Field(default=None, min_length=32, max_length=32, pattern=r'^[0-9a-f]{32}$')


def validate_hire_eligibility(db, application_id: int):
    """Validate the same hiring prerequisites for every entry point."""
    application = get_application(db, application_id)
    if application['status'] != 'offered':
        raise HTTPException(status_code=409, detail='Decision is not allowed from current status')
    if not db.execute("SELECT 1 FROM users WHERE id=? AND role='candidate' AND active=1",
                      (application['candidate_id'],)).fetchone():
        raise HTTPException(status_code=409, detail='An active candidate account is required')
    offer = db.execute("SELECT * FROM job_offers WHERE application_id=? AND recipient_id=? AND status='accepted'",
                       (application_id, application['candidate_id'])).fetchone()
    if offer is None:
        raise HTTPException(status_code=409, detail='Candidate must accept an offer before hiring')
    if _upcoming_interview(db, application_id):
        raise HTTPException(status_code=409, detail='Resolve active interviews before hiring')
    return application, offer


def _release_finalized_slots(db, application_id: int, actor_id: int):
    now = utc_text(datetime.now(UTC))
    slots = db.execute("""SELECT s.id FROM interview_slots s
        LEFT JOIN interviews i ON i.id=s.interview_id
        WHERE s.target_application_id=? AND s.withdrawn=0 AND s.start_at>?
          AND (s.interview_id IS NULL OR i.status='cancelled')""", (application_id, now)).fetchall()
    for slot in slots:
        db.execute('UPDATE interview_slots SET withdrawn=1 WHERE id=?', (slot['id'],))
        db.execute('INSERT INTO slot_audit_events(slot_id,actor_id,action) VALUES (?,?,?)',
                   (slot['id'], actor_id, 'slot_withdrawn'))


def record_hire(db, application_id: int, actor_id: int, note: str = ''):
    """Atomically hire after rechecking the application and accepted offer."""
    validate_hire_eligibility(db, application_id)
    db.execute("UPDATE applications SET status='hired', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?", (application_id,))
    details = json.dumps({'decision': 'hire', 'note': note}, ensure_ascii=False)
    db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,'decision',?)",
               (application_id, actor_id, details))
    _release_finalized_slots(db, application_id, actor_id)


@router.post("/applications/{application_id}/decision")
def decide(application_id: int, payload: Decision, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_current_role(db, request, user, 'hr')
        application = get_application(db, application_id)
        if payload.review_reference is not None:
            reference = db.execute('SELECT * FROM hr_review_references WHERE reference=?',
                                   (payload.review_reference,)).fetchone()
            if (reference is None or reference['application_id'] != application_id or reference['actor_id'] != user.id
                    or reference['session_hash'] != token_digest(request.cookies.get(COOKIE_NAME, ''))
                    or reference['expires_at'] <= int(time.time()) or reference['job_id'] != application['job_id']
                    or reference['status'] != application['status']
                    or reference['revision'] != _review_revision(db, application_id)
                    or reference['status_revision'] != _review_status_revision(db, application_id)):
                raise HTTPException(status_code=409, detail='Review expired or changed; review again before deciding.')
            if payload.decision == 'reject' and not payload.note.strip():
                raise HTTPException(status_code=422, detail='A rejection reason is required for a reviewed decision.')
            db.execute('DELETE FROM hr_review_references WHERE reference=?', (payload.review_reference,))
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
            record_hire(db, application_id, user.id, payload.note)
            _require_current_role(db, request, user, 'hr')
            return application_view(get_application(db, application_id), user)
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
        if payload.decision == 'reject':
            _release_finalized_slots(db, application_id, user.id)
        _require_current_role(db, request, user, 'hr')
        return application_view(get_application(db, application_id), user)


@router.get("/audit")
def audit(request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        return [dict(row) for row in db.execute("SELECT * FROM audit_events ORDER BY id DESC")]
