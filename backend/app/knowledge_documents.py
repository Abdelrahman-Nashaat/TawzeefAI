"""Local HR-managed knowledge documents; content is always untrusted data."""

import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from app.auth import User, require_roles
from app.db import connect
from app.document_text import (
    MAX_FILE_BYTES,
    MAX_TEXT_CHARS,
    OCR_NOTICE,
    DocumentTextError,
    extract_docx_sections,
    extract_pdf_pages,
    extract_pdf_pages_with_ocr,
    require_ocr_enabled,
)

router = APIRouter(prefix="/api/knowledge")
HR = Annotated[User, Depends(require_roles("hr"))]
Audience = Literal["public", "staff", "hr"]
Collection = Literal["legacy_unclassified", "handbook", "job_description", "past_hire_patterns"]


def initialize_knowledge_documents(path: str | Path, *, include_samples: bool = False) -> None:
    with connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                audience TEXT NOT NULL CHECK (audience IN ('public', 'staff', 'hr')),
                filename TEXT,
                text TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS knowledge_documents_audience ON knowledge_documents(audience);
            CREATE TABLE IF NOT EXISTS knowledge_document_audit (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES knowledge_documents(id),
                actor_id INTEGER NOT NULL REFERENCES users(id),
                action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS knowledge_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                include_sample_policies INTEGER NOT NULL CHECK (include_sample_policies IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS knowledge_settings_audit (
                id INTEGER PRIMARY KEY,
                actor_id INTEGER NOT NULL REFERENCES users(id),
                include_sample_policies INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            """
        )
        columns = {row['name'] for row in db.execute('PRAGMA table_info(knowledge_documents)')}
        if 'active' not in columns:
            db.execute('ALTER TABLE knowledge_documents ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))')
        if 'pages_json' not in columns:
            db.execute('ALTER TABLE knowledge_documents ADD COLUMN pages_json TEXT')
        if 'collection' not in columns:
            db.execute("""ALTER TABLE knowledge_documents ADD COLUMN collection TEXT NOT NULL
                DEFAULT 'legacy_unclassified' CHECK (collection IN
                ('legacy_unclassified', 'handbook', 'job_description', 'past_hire_patterns'))""")
        if 'job_id' not in columns:
            db.execute('ALTER TABLE knowledge_documents ADD COLUMN job_id TEXT REFERENCES jobs(id)')
        if 'sections_json' not in columns:
            db.execute('ALTER TABLE knowledge_documents ADD COLUMN sections_json TEXT')
        if 'ocr_pages_json' not in columns:
            db.execute('ALTER TABLE knowledge_documents ADD COLUMN ocr_pages_json TEXT')
        db.execute('INSERT OR IGNORE INTO knowledge_settings (id, include_sample_policies) VALUES (1, ?)', (int(include_samples),))


def clean_title(value: str) -> str:
    title = " ".join(value.split())
    if not title or len(title) > 200 or any(ord(char) < 32 for char in title):
        raise ValueError("Title must contain 1–200 printable characters")
    return title


def clean_filename(value: str) -> str:
    # Store only a basename; never use this value as a filesystem path.
    filename = PurePosixPath(value.replace("\\", "/")).name
    if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
        raise ValueError("Invalid filename")
    return filename


class TextDocument(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    audience: Audience
    collection: Collection = 'legacy_unclassified'
    job_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return clean_title(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Document text cannot be blank or contain null characters")
        return value.strip()


def store_document(path: str | Path, user: User, title: str, audience: str, text: str, filename: str | None,
                   pages: list[dict] | None = None, collection: Collection = 'legacy_unclassified',
                   job_id: str | None = None, sections: list[dict] | None = None,
                   ocr_pages: list[int] | None = None) -> dict:
    if collection not in ('legacy_unclassified', 'handbook', 'job_description', 'past_hire_patterns'):
        raise ValueError('Unsupported knowledge collection')
    # Historical hire patterns must never become public through their audience.
    if collection == 'past_hire_patterns':
        audience = 'hr'
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        if job_id is not None and not db.execute('SELECT 1 FROM jobs WHERE id = ?', (job_id,)).fetchone():
            raise HTTPException(status_code=422, detail='Unknown job identifier')
        cursor = db.execute(
            "INSERT INTO knowledge_documents (title,audience,filename,text,created_by,pages_json,collection,job_id,sections_json,ocr_pages_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (title, audience, filename, text, user.id, json.dumps(pages, ensure_ascii=False) if pages is not None else None, collection, job_id, json.dumps(sections, ensure_ascii=False) if sections is not None else None, json.dumps(ocr_pages) if ocr_pages is not None else None),
        )
        db.execute(
            "INSERT INTO knowledge_document_audit (document_id,actor_id,action) VALUES (?,?,'created')",
            (cursor.lastrowid, user.id),
        )
        row = db.execute(
            "SELECT id,title,audience,collection,job_id,filename,created_at FROM knowledge_documents WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        result = dict(row)
        if pages is not None:
            result.update(page_count=len(pages), pages_with_text=sum(bool(page['text']) for page in pages),
                          notice=OCR_NOTICE if ocr_pages is not None else 'Only extractable text is indexed; blank or scanned pages are not processed with OCR.')
        result['ocr_pages'] = ocr_pages
        return result


def authorized_documents_in_db(db, role: str) -> list[dict]:
    """Apply role and collection restrictions in the caller's transaction."""
    allowed = {"candidate": ("public",), "employee": ("public", "staff"), "hr": ("public", "staff", "hr")}.get(role, ())
    if not allowed:
        return []
    placeholders = ",".join("?" for _ in allowed)
    rows = db.execute(
        f"""SELECT id,title,audience,text,pages_json,collection,job_id,sections_json,ocr_pages_json FROM knowledge_documents
            WHERE active = 1 AND audience IN ({placeholders}) AND
            (collection IN ('legacy_unclassified','handbook','job_description') OR
             (collection = 'past_hire_patterns' AND ? = 'hr')) ORDER BY id""",
        (*allowed, role),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_authorized_documents(path: str | Path, role: str) -> list[dict]:
    """Return documents permitted for role; unknown roles get no documents."""
    with connect(path) as db:
        return authorized_documents_in_db(db, role)


@router.get("/documents")
def list_documents(request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        rows = db.execute(
            "SELECT id,title,audience,collection,job_id,filename,created_at,active,ocr_pages_json FROM knowledge_documents ORDER BY id DESC"
        )
        return [{**{k: row[k] for k in row.keys() if k != 'ocr_pages_json'}, 'active': bool(row['active']),  # noqa: SIM118
                 'ocr_pages': json.loads(row['ocr_pages_json']) if row['ocr_pages_json'] is not None else None} for row in rows]


@router.post('/documents/{document_id}/deactivate')
def deactivate_document(document_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM knowledge_documents WHERE id = ?', (document_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Document not found')
        if not row['active']:
            raise HTTPException(status_code=409, detail='Document is already inactive')
        db.execute('UPDATE knowledge_documents SET active = 0 WHERE id = ?', (document_id,))
        db.execute(
            "INSERT INTO knowledge_document_audit (document_id,actor_id,action) VALUES (?,?,'deactivated')",
            (document_id, user.id),
        )
        return {'id': row['id'], 'title': row['title'], 'audience': row['audience'], 'collection': row['collection'], 'job_id': row['job_id'],
                'filename': row['filename'], 'created_at': row['created_at'], 'active': False}


def include_sample_policies(path: str | Path) -> bool:
    with connect(path) as db:
        row = db.execute('SELECT include_sample_policies FROM knowledge_settings WHERE id = 1').fetchone()
        return bool(row['include_sample_policies']) if row is not None else False


class KnowledgeSettings(BaseModel):
    include_sample_policies: bool = Field(strict=True)


@router.get('/settings')
def get_settings(request: Request, user: HR):
    return {'include_sample_policies': include_sample_policies(request.app.state.database_path)}


@router.post('/settings')
def set_settings(payload: KnowledgeSettings, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE knowledge_settings SET include_sample_policies = ? WHERE id = 1',
                   (int(payload.include_sample_policies),))
        db.execute('INSERT INTO knowledge_settings_audit (actor_id,include_sample_policies) VALUES (?,?)',
                   (user.id, int(payload.include_sample_policies)))
    return {'include_sample_policies': payload.include_sample_policies}


@router.post("/documents/text", status_code=201)
def add_text_document(payload: TextDocument, request: Request, user: HR):
    return store_document(request.app.state.database_path, user, payload.title, payload.audience, payload.text, None,
                          collection=payload.collection, job_id=payload.job_id)


@router.post("/documents", status_code=201)
async def add_document(request: Request, user: HR, title: str, audience: Audience, filename: str,
                       collection: Collection = 'legacy_unclassified',
                       job_id: Annotated[str | None, Query(min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')] = None,
                       ocr: bool = False, ocr_language: Literal['eng', 'eng+ara'] = 'eng'):
    try:
        title = clean_title(title)
        filename = clean_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds 5 MiB limit")
        chunks.extend(chunk)
    try:
        ocr_pages = None
        if ocr:
            require_ocr_enabled()
            pages, ocr_pages = await run_in_threadpool(extract_pdf_pages_with_ocr, filename, bytes(chunks), ocr_language)
            text = '\n'.join(page['text'] for page in pages if page['text'])
            sections = None
        elif filename.lower().endswith('.pdf'):
            pages = await run_in_threadpool(extract_pdf_pages, filename, bytes(chunks))
            text = '\n'.join(page['text'] for page in pages if page['text'])
            sections = None
        else:
            pages = None
            text, sections = await run_in_threadpool(extract_docx_sections, filename, bytes(chunks))
    except DocumentTextError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    return store_document(request.app.state.database_path, user, title, audience, text, filename, pages,
                          collection=collection, job_id=job_id, sections=sections, ocr_pages=ocr_pages)
