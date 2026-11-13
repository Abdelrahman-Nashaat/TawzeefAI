"""Role-filtered knowledge search for the PostgreSQL demo."""
import asyncio
import hashlib
import json
import math
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event, Lock
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from psycopg import sql
from pydantic import BaseModel, Field, field_validator

from app.ai_provider import ProviderUnavailable, generate_with_groq
from app.auth import User
from app.document_text import MAX_FILE_BYTES, DocumentTextError, extract_pdf_pages
from app.knowledge import (
    ROLE_AUDIENCES,
    Source,
    _document_passages,
    _has_valid_citations,
    _rank_lexical,
    _tokens,
)
from app.knowledge_documents import annotate_pdf_selected_pages, pdf_preview_digest

_HYBRID_BUSY = BoundedSemaphore(1)
_GENERATION_BUSY = BoundedSemaphore(1)
_HYBRID_MAX_PASSAGES = 200
_PDF_EXTRACTION_SLOTS = BoundedSemaphore(2)
_PDF_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='knowledge-pdf')
_PDF_SUBMIT_LOCK = Lock()
_PDF_EXECUTOR_FAILED = False


def _extract_pdf_with_digest(filename, content):
    pages = extract_pdf_pages(filename, content)
    return pages, pdf_preview_digest(pages, None, content, None), hashlib.sha256(content).hexdigest()


def _unique_policy_title(question, sources):
    """Offer one authorized title match as an unverified excerpt, or abstain."""
    terms = _tokens(question)
    if len(terms) != 1 or not re.search(r'(?<!\w)policy(?!\w)', question, re.IGNORECASE):
        return []
    term = next(iter(terms))
    if len(term) < 4 or term not in re.findall(r'[^\W_]+', question.casefold()):
        return []
    matches = [source for source in sources if term in re.findall(r'[^\W_]+', source.title.casefold())
               and re.search(r'(?<!\w)policy(?!\w)', source.title, re.IGNORECASE)]
    return [(1.0, matches[0])] if len(matches) == 1 else []


def _vector_literal(values):
    values = [float(value) for value in values]
    if len(values) != 384 or not all(math.isfinite(value) for value in values) or not any(values):
        raise ValueError('Invalid embedding')
    return '[' + ','.join(str(value) for value in values) + ']'


def _rerank_lexical_with_semantic(lexical, semantic):
    """Fuse ranks only for lexically eligible sources; semantic hits are not proof.

    Reciprocal rank fusion avoids treating incomparable BM25 and cosine values
    as if they shared a scale. Ties retain lexical order. Sources appearing
    only in semantic search remain separate, unverified suggestions.
    """
    semantic_ranks = {}
    for position, (_, source) in enumerate(semantic, 1):
        semantic_ranks.setdefault(source.id, position)
    def fused(item):
        position, (_, source) = item
        score = 1 / (60 + position)
        if source.id in semantic_ranks:
            score += 1 / (60 + semantic_ranks[source.id])
        return -score, position
    return [item for _, item in sorted(enumerate(lexical, 1), key=fused)]


def _load_embedder(directory):
    from app.hybrid_api import _load_model
    return _load_model(directory)


def _semantic_candidates(db, sources, question, model):
    """Persist source-bound vectors and search only caller-authorized source IDs.

    The operator must provision pgvector. Embeddings are retained in the demo
    schema; this path must be used only with synthetic documents.
    """
    if len(sources) > _HYBRID_MAX_PASSAGES:
        raise ValueError('Experimental passage limit exceeded')
    if not sources:
        return []
    extension = db.execute('''SELECT n.nspname FROM pg_extension e
        JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector' ''').fetchone()
    if not extension:
        raise RuntimeError('pgvector is unavailable')
    namespace = sql.Identifier(extension['nspname'])
    vectors = model.encode([source.text for source in sources], query=False)
    query = model.encode([question], query=True)
    if len(vectors) != len(sources) or len(query) != 1:
        raise ValueError('Embedding count does not match')
    # Serialize lazy table creation and index refreshes within this schema.
    db.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('knowledge-vector-v1'))")
    db.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS knowledge_vectors (
        source_id text PRIMARY KEY, revision text NOT NULL, embedding {}.vector(384) NOT NULL)''').format(namespace))
    allowed = []
    for source, vector in zip(sources, vectors, strict=True):
        revision = hashlib.sha256(source.text.encode('utf-8')).hexdigest()
        allowed.append(source.id)
        db.execute(sql.SQL('''INSERT INTO knowledge_vectors(source_id,revision,embedding)
            VALUES (%s,%s,%s::{}.vector) ON CONFLICT(source_id) DO UPDATE
            SET revision=EXCLUDED.revision,embedding=EXCLUDED.embedding''').format(namespace),
            (source.id, revision, _vector_literal(vector)))
    rows = db.execute(sql.SQL('''SELECT source_id, embedding OPERATOR({}.<=>) %s::{}.vector AS distance
        FROM knowledge_vectors WHERE source_id = ANY(%s) ORDER BY distance,source_id LIMIT 3''').format(
            namespace, namespace), (_vector_literal(query[0]), allowed)).fetchall()
    # Similarity is a retrieval hint, not a factual-support or safety score.
    by_id = {source.id: source for source in sources}
    return [(1 - row['distance'], by_id[row['source_id']]) for row in rows if 1 - row['distance'] >= 0.80]

DDL = '''CREATE TABLE IF NOT EXISTS knowledge_documents (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    title text NOT NULL, body text NOT NULL, audience text NOT NULL
        CHECK(audience IN ('public','staff','hr')),
    collection text NOT NULL CHECK(collection IN ('handbook','job_description','past_hire_patterns')),
    page integer CHECK(page IS NULL OR page > 0),
    created_by bigint NOT NULL REFERENCES users(id), active boolean NOT NULL DEFAULT true)'''
AUDIT_DDL = '''CREATE TABLE IF NOT EXISTS knowledge_audit (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES knowledge_documents(id),
    actor_id bigint NOT NULL REFERENCES users(id), action text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp())'''


class Metadata(BaseModel):
    job_id: str | None = Field(default=None, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    section: str | None = Field(default=None, max_length=200)

    @field_validator('section')
    @classmethod
    def valid_section(cls, value):
        if value is None:
            return None
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError('Section must be a nonempty single-line value')
        return value.strip()


def source_dict(source, metadata):
    result = {'id': source.id, 'title': source.title, 'page': source.page, 'text': source.text}
    row = metadata[source.id]
    for key in ('job_id', 'section'):
        if row[key] is not None:
            result[key] = row[key]
    return result


class Document(Metadata):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    audience: Literal['public', 'staff', 'hr']
    collection: Literal['handbook', 'job_description', 'past_hire_patterns']
    page: int | None = Field(default=None, ge=1, le=100000)


class Question(Metadata):
    question: str = Field(min_length=1, max_length=2000)
    collection: Literal['handbook', 'job_description', 'past_hire_patterns'] | None = None
    experimental_hybrid: bool = False
    generate_answer: bool = False



async def _read_pdf(request, filename):
    global _PDF_EXECUTOR_FAILED
    if not _PDF_EXTRACTION_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=503, detail='PDF processing is busy; try again')
    slot_owned = True
    try:
        with _PDF_SUBMIT_LOCK:
            if _PDF_EXECUTOR_FAILED:
                raise HTTPException(status_code=503, detail='PDF processing is unavailable; restart the server')
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_FILE_BYTES:
                raise HTTPException(status_code=413, detail='File exceeds 5 MiB limit')
            content.extend(chunk)
        # A cancelled request must not release capacity while its worker is
        # still extracting. The concurrent future completes only when the
        # underlying thread finishes (or a queued job is cancelled).
        gate = Event()
        admitted = False
        def guarded_extract():
            gate.wait()
            if admitted:
                return _extract_pdf_with_digest(filename, bytes(content))
            # submit() may queue work before raising; never execute an orphan.
            return None
        with _PDF_SUBMIT_LOCK:
            if _PDF_EXECUTOR_FAILED:
                raise HTTPException(status_code=503, detail='PDF processing is unavailable; restart the server')
            try:
                future = _PDF_EXECUTOR.submit(guarded_extract)
            except BaseException as exc:
                _PDF_EXECUTOR_FAILED = True
                gate.set()
                if isinstance(exc, Exception):
                    raise HTTPException(status_code=503, detail='PDF processing is unavailable; restart the server') from exc
                raise
            future.add_done_callback(lambda _: _PDF_EXTRACTION_SLOTS.release())
            slot_owned = False
            admitted = True
            gate.set()
        try:
            return await asyncio.wrap_future(future)
        except DocumentTextError as exc:
            raise HTTPException(status_code=422, detail={'code': exc.code, 'message': str(exc)}) from exc
    finally:
        if slot_owned:
            _PDF_EXTRACTION_SLOTS.release()


def install_routes(app, connect, authenticate, authorize_locked):
    @app.post('/api/knowledge/documents/pdf-preview')
    async def preview_pdf(request: Request, user: Annotated[User, Depends(authenticate)], filename: str = 'document.pdf'):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
        pages, digest, _ = await _read_pdf(request, filename)
        if sum(len(page['text'].splitlines()) for page in pages) > 5000:
            raise HTTPException(status_code=422, detail='PDF has too many lines to review')
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
        return {'pages': [dict(page, lines=page['text'].splitlines()) for page in pages], 'digest': digest}

    @app.post('/api/knowledge/documents/pdf', status_code=201)
    async def create_pdf(request: Request, user: Annotated[User, Depends(authenticate)],
                         filename: str, title: str, audience: Literal['public', 'staff', 'hr'],
                         collection: Literal['handbook', 'job_description', 'past_hire_patterns'],
                         digest: str, marks: str = '[]', job_id: str | None = None):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
        if not title.strip() or len(title) > 200 or len(marks) > 10000:
            raise HTTPException(status_code=422, detail='Invalid title or heading selection')
        if collection == 'past_hire_patterns' and audience != 'hr':
            raise HTTPException(status_code=422, detail='Past hire patterns must be HR-only')
        if job_id is not None and (len(job_id) > 64 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', job_id)):
            raise HTTPException(status_code=422, detail='Invalid job ID')
        pages, actual_digest, source_hash = await _read_pdf(request, filename)
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or not secrets.compare_digest(actual_digest, digest):
            raise HTTPException(status_code=409, detail='PDF differs from the reviewed preview')
        try:
            selected = json.loads(marks)
            if not isinstance(selected, list):
                raise TypeError('Invalid heading selection')
            if selected:
                annotated = annotate_pdf_selected_pages(pages, selected)
            else:
                annotated = [dict(page, selected_sections_v1=[{'start': 0, 'end': len(page['text']), 'section': None}]) for page in pages]
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        segments = []
        for page in annotated:
            for span in page['selected_sections_v1']:
                text = page['text'][span['start']:span['end']]
                if not text.strip():
                    continue
                for offset in range(0, len(text), 4000):
                    part = text[offset:offset + 4000]
                    segments.append((page['page'], span['section'], part))
        if not segments or len(segments) > 100:
            raise HTTPException(status_code=422, detail='PDF must contain 1 to 100 text segments')
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            db.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('knowledge-documents-v1'))")
            count = db.execute('SELECT count(*) AS n FROM knowledge_documents WHERE active').fetchone()['n']
            if count + len(segments) > 500:
                raise HTTPException(status_code=409, detail='Experimental knowledge capacity reached')
            ids = []
            for page, section, body in segments:
                row = db.execute('''INSERT INTO knowledge_documents(title,body,audience,collection,page,job_id,section,created_by,source_sha256)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                    (title.strip(), body, audience, collection, page, job_id, section, user.id, source_hash)).fetchone()
                ids.append(row['id'])
                db.execute("INSERT INTO knowledge_audit(document_id,actor_id,action) VALUES (%s,%s,'created')", (row['id'], user.id))
            authorize_locked(db, request, user, 'hr')
        return {'ids': ids, 'segments': len(ids)}

    @app.get('/api/knowledge/documents')
    def list_documents(request: Request, user: Annotated[User, Depends(authenticate)]):
        """List active document metadata for HR without returning document bodies."""
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            rows = db.execute('''SELECT id,title,audience,collection,page,job_id,section FROM knowledge_documents
                WHERE active ORDER BY id DESC LIMIT 500 FOR SHARE''').fetchall()
            authorize_locked(db, request, user, 'hr')
            return {'items': [{key: value for key, value in dict(row).items() if key not in ('job_id', 'section') or value is not None} for row in rows]}

    @app.post('/api/knowledge/documents/text', status_code=201)
    def create(payload: Document, request: Request, user: Annotated[User, Depends(authenticate)]):
        title, body = payload.title.strip(), payload.text.strip()
        if not title or not body:
            raise HTTPException(status_code=422, detail='Title and text are required')
        if payload.collection == 'past_hire_patterns' and payload.audience != 'hr':
            raise HTTPException(status_code=422, detail='Past hire patterns must be HR-only')
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            db.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('knowledge-documents-v1'))")
            if db.execute('SELECT count(*) AS n FROM knowledge_documents WHERE active').fetchone()['n'] >= 500:
                raise HTTPException(status_code=409, detail='Experimental knowledge capacity reached')
            row = db.execute('''INSERT INTO knowledge_documents(title,body,audience,collection,page,job_id,section,created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,title,audience,collection,page,job_id,section''',
                (title, body, payload.audience, payload.collection, payload.page, payload.job_id, payload.section, user.id)).fetchone()
            db.execute("INSERT INTO knowledge_audit(document_id,actor_id,action) VALUES (%s,%s,'created')", (row['id'], user.id))
            authorize_locked(db, request, user, 'hr')
            return {key: value for key, value in dict(row).items() if key not in ('job_id', 'section') or value is not None}

    @app.post('/api/knowledge/documents/{document_id}/deactivate')
    def deactivate(document_id: int, request: Request, user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            doc = db.execute('UPDATE knowledge_documents SET active=false WHERE id=%s AND active RETURNING id', (document_id,)).fetchone()
            if not doc:
                raise HTTPException(status_code=404, detail='Document not found')
            db.execute("INSERT INTO knowledge_audit(document_id,actor_id,action) VALUES (%s,%s,'deactivated')", (document_id, user.id))
            authorize_locked(db, request, user, 'hr')
            return {'id': document_id, 'active': False}

    @app.post('/api/knowledge/answer')
    def answer(payload: Question, request: Request, user: Annotated[User, Depends(authenticate)]):
        if not payload.question.strip():
            raise HTTPException(status_code=422, detail='Question is required')
        if payload.generate_answer and (os.environ.get('TAWZEEF_POSTGRES_ENABLE_GROQ') != '1'
                                        or not os.environ.get('GROQ_API_KEY', '').strip()):
            raise HTTPException(status_code=503, detail='External answer generation is not configured')
        if payload.experimental_hybrid:
            directory = os.environ.get('TAWZEEF_EMBEDDING_MODEL_DIR', '')
            if os.environ.get('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID') != '1' or not directory:
                raise HTTPException(status_code=503, detail='Experimental hybrid search is not configured')
            if not _HYBRID_BUSY.acquire(blocking=False):
                raise HTTPException(status_code=429, detail='Experimental hybrid search is busy')
        generation_acquired = False
        try:
            if payload.generate_answer:
                generation_acquired = _GENERATION_BUSY.acquire(blocking=False)
                if not generation_acquired:
                    raise HTTPException(status_code=429, detail='External answer generation is busy')
            return search(payload, request, user)
        finally:
            if generation_acquired:
                _GENERATION_BUSY.release()
            if payload.experimental_hybrid:
                _HYBRID_BUSY.release()

    def search(payload, request, user):
        with connect() as db:
            authorize_locked(db, request, user)
            # Filter before retrieval and keep selected records locked until the
            # session recheck so a revoked document cannot escape mid-response.
            rows = db.execute('''SELECT id,title,body,page,collection,job_id,section FROM knowledge_documents
                WHERE active AND audience = ANY(%s) AND (collection <> 'past_hire_patterns' OR %s = 'hr')
                AND (%s::text IS NULL OR collection=%s)
                AND (%s::text IS NULL OR job_id=%s) AND (%s::text IS NULL OR section=%s)
                ORDER BY id LIMIT 500 FOR SHARE''',
                (list(ROLE_AUDIENCES[user.role]), user.role, payload.collection, payload.collection,
                 payload.job_id, payload.job_id, payload.section, payload.section)).fetchall()
            metadata = {}
            if payload.experimental_hybrid:
                sources = []
                for row in rows:
                    for passage in _document_passages({'id': row['id'], 'title': row['title'], 'text': row['body']}):
                        sources.append(Source(id=passage.id, title=passage.title, text=passage.text, page=row['page']))
                        metadata[passage.id] = row
            else:
                sources = [Source(id=f"pg-doc-{r['id']}", title=r['title'], text=r['body'], page=r['page']) for r in rows]
                metadata = {source.id: row for source, row in zip(sources, rows, strict=True)}
            ranked = _rank_lexical(_tokens(payload.question), sources)
            if not ranked and not payload.experimental_hybrid:
                ranked = _unique_policy_title(payload.question, sources)
            suggestions = []
            if payload.experimental_hybrid:
                try:
                    model = _load_embedder(os.environ['TAWZEEF_EMBEDDING_MODEL_DIR'])
                    semantic = _semantic_candidates(db, sources, payload.question, model)
                except Exception as exc:
                    raise HTTPException(status_code=503, detail='Experimental hybrid search is unavailable') from exc
                ranked = _rerank_lexical_with_semantic(ranked, semantic)
            ranked = ranked[:3]
            if payload.experimental_hybrid:
                lexical_ids = {source.id for _, source in ranked}
                suggestions = [source for _, source in semantic if source.id not in lexical_ids]
            authorize_locked(db, request, user)
            selected = [source for _, source in ranked]
            answer_text = None
            mode = 'extractive' if ranked else 'abstain'
            notice = ('Experimental semantic ranking and leads are unverified; review each source. No external service was called.'
                      if payload.experimental_hybrid else
                      'Synthetic local excerpts only; verify the cited source. No model or external service was called.')
            if payload.generate_answer and selected:
                try:
                    answer_text = generate_with_groq(payload.question, [
                        {'id': source.id, 'title': source.title, 'text': source.text[:4000]} for source in selected
                    ])
                    if _has_valid_citations(answer_text, selected):
                        mode = 'groq'
                        notice = ('AI-generated using excerpts sent to Groq; valid citation IDs do not establish factual '
                                  'accuracy. Verify every claim against the sources.')
                    else:
                        answer_text = None
                        notice = 'AI citations could not be verified after sending excerpts to Groq; showing unverified source excerpts.'
                except ProviderUnavailable:
                    answer_text = None
                    notice = 'External generation was unavailable; the question and excerpts may have been sent to Groq. Showing unverified source excerpts.'
                # Recheck the session after provider latency before disclosing results.
                authorize_locked(db, request, user)
            return {'mode': mode, 'answer': answer_text,
                    'sources': [source_dict(source, metadata) for _, source in ranked],
                    'suggestions': [source_dict(source, metadata) for source in suggestions],
                    'notice': notice}
