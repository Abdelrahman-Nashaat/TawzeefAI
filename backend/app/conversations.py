"""Owner-only local conversation history with revocable source visibility.

Model calls are outside SQLite transactions. Before committing a reply, and
again before reading it, all passages sent to the model are re-authorized.
"""

import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.auth import COOKIE_NAME, User, get_current_user, token_digest
from app.db import connect
from app.knowledge import (
    DEFAULT_FIXTURES,
    ROLE_AUDIENCES,
    Source,
    _document_passages,
    _documents,
    answer_question,
    is_context_followup,
)
from app.knowledge_documents import authorized_documents_in_db
from app.workflow import candidate_stage

router = APIRouter(prefix='/api/conversations')
Authenticated = Annotated[User, Depends(get_current_user)]


def initialize_conversations(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner_id, id);
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                mode TEXT NOT NULL,
                notice TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS conversation_messages_owner ON conversation_messages(conversation_id, id);
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(conversation_messages)')}
        if 'context_role' not in columns:
            # Old messages cannot prove which role authored them; they remain
            # visible under existing rules but are never used as model context.
            db.execute('ALTER TABLE conversation_messages ADD COLUMN context_role TEXT')
        if 'source_scope' not in columns:
            # Earlier messages used the general knowledge collection.
            db.execute("ALTER TABLE conversation_messages ADD COLUMN source_scope TEXT NOT NULL DEFAULT 'general'")
        if 'status_application_id' not in columns:
            db.execute('ALTER TABLE conversation_messages ADD COLUMN status_application_id INTEGER')


class ConversationCreate(BaseModel):
    title: str = Field(default='محادثة جديدة', min_length=1, max_length=120)

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Title cannot be blank')
        return value.strip()


class MessageCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal['general', 'policy', 'application_status'] = 'general'
    application_id: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode='after')
    def validate_status_target(self):
        if (self.mode == 'application_status') != (self.application_id is not None):
            raise ValueError('An application is required only for status questions')
        return self

    @field_validator('question')
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Question cannot be blank')
        return value.strip()


def _owned(db, conversation_id: int, user: User, request: Request):
    row = db.execute('SELECT id,title,created_at FROM conversations WHERE id = ? AND owner_id = ?',
                     (conversation_id, user.id)).fetchone()
    if row is None:
        # Avoid revealing another user's conversation existence.
        raise HTTPException(status_code=404, detail='Conversation not found')
    current = db.execute('SELECT role,username,active FROM users WHERE id = ?', (user.id,)).fetchone()
    if current is None or not current['active'] or current['role'] != user.role or current['username'] != user.username:
        raise HTTPException(status_code=403, detail='Account permissions changed')
    token = request.cookies.get(COOKIE_NAME, '')
    session = db.execute('SELECT 1 FROM sessions WHERE token_hash = ? AND user_id = ? AND expires_at > ?',
                         (token_digest(token), user.id, int(time.time()))).fetchone()
    if session is None:
        raise HTTPException(status_code=401, detail='Session no longer active')
    return row


def _authorized_sources(db, role: str) -> dict:
    allowed = ROLE_AUDIENCES.get(role, set())
    if not allowed:
        return {}
    setting = db.execute('SELECT include_sample_policies FROM knowledge_settings WHERE id = 1').fetchone()
    samples = bool(setting and setting['include_sample_policies'])
    sources = _documents(DEFAULT_FIXTURES, role, include_samples=samples, include_fixture_jobs=False)
    for row in db.execute('SELECT * FROM jobs ORDER BY id'):
        fields = [row['title'], row['location']]
        for field in ('required_skills', 'preferred_skills', 'responsibilities'):
            fields.extend(json.loads(row[field]))
        sources.append(Source(id=f"job-db-{row['id']}", title=row['title'], text='; '.join(fields)))
    for row in authorized_documents_in_db(db, role):
        sources.extend(_document_passages(row))
    # Ambiguous IDs must never authorize a stale stored response.
    by_id = {source.id: source for source in sources}
    if len(by_id) != len(sources):
        return {}
    return by_id


def _policy_sources(db, role: str) -> dict:
    from app.policy_agent import authorized_policy_sources

    sources = authorized_policy_sources(db, role)
    by_id = {source.id: source for source in sources}
    return by_id if len(by_id) == len(sources) else {}


def _visible_message(row, available: dict, policy_available: dict | None = None) -> dict:
    try:
        if row['source_scope'] == 'policy':
            available = policy_available or {}
        elif row['source_scope'] != 'general':
            raise ValueError('Invalid source scope')
        stored = json.loads(row['sources_json'])
        if not isinstance(stored, list) or any(not isinstance(item, dict) for item in stored):
            raise ValueError('Invalid sources')
        # Empty references are permitted only for an abstention.
        safe = (bool(stored) or row['mode'] == 'abstain') and all(
            item.get('id') in available and item.get('digest') == _digest(available[item['id']])
            for item in stored
        )
    except (TypeError, ValueError, KeyError):
        stored, safe = [], False
    result = {'id': row['id'], 'question': row['question'], 'created_at': row['created_at']}
    if safe:
        result.update({'answer': row['answer'], 'sources': [asdict(available[item['id']]) for item in stored],
                       'mode': row['mode'], 'notice': row['notice']})
    else:
        result.update({'answer': 'This answer is unavailable because its sources are no longer accessible.',
                       'sources': [], 'mode': 'unavailable', 'notice': None})
    return result


_STAGE_LABELS = {'applied': 'تم التقديم', 'screened': 'تم فحص الطلب',
                 'shortlisted': 'تمت الموافقة على الانتقال للمرحلة التالية',
                 'interview_scheduled': 'تم تسجيل موعد مقابلة', 'interviewed': 'تم تسجيل إجراء المقابلة',
                 'offered': 'عرض توظيف', 'hired': 'تم التوظيف', 'rejected': 'انتهى الطلب بالرفض'}


def _visible_status_message(db, row, user: User) -> dict:
    result = {'id': row['id'], 'question': row['question'], 'created_at': row['created_at']}
    try:
        if user.role != 'candidate' or row['context_role'] != 'candidate' or not isinstance(row['status_application_id'], int):
            raise ValueError('Status is unavailable')
        current = candidate_stage(db, row['status_application_id'], user.id)
        label = _STAGE_LABELS[current['stage']]
        result.update({'mode': 'application_status', **current, 'answer': f'مرحلة الطلب الحالية: {label}.',
                       'sources': [], 'notice': 'تُعرض المرحلة الحالية عند فتح المحادثة وقد تتغير.'})
    except (HTTPException, ValueError, KeyError):
        result.update({'mode': 'unavailable', 'answer': 'حالة هذا الطلب لم تعد متاحة.', 'sources': [], 'notice': None})
    return result


def _digest(source: Source) -> str:
    data = asdict(source)
    if data['page'] is None:
        # Preserve digests for historical messages written before page metadata.
        del data['page']
    if data['section'] is None:
        del data['section']
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _context_still_allowed(db, user: User, conversation_id: int, previous: dict) -> bool:
    row = db.execute('SELECT * FROM conversation_messages WHERE id=? AND conversation_id=?',
                     (previous['id'], conversation_id)).fetchone()
    if row is None or any(row[key] != previous[key] for key in ('question', 'sources_json', 'mode', 'context_role', 'source_scope')):
        return False
    if row['context_role'] != user.role or row['mode'] not in ('extractive', 'groq'):
        return False
    policy_available = _policy_sources(db, user.role) if row['source_scope'] == 'policy' else None
    visible = _visible_message(row, _authorized_sources(db, user.role), policy_available)
    return visible['mode'] in ('extractive', 'groq') and bool(visible['sources'])


def _assert_conversation_head(db, conversation_id: int, previous: dict | None) -> None:
    row = db.execute('SELECT id FROM conversation_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1',
                     (conversation_id,)).fetchone()
    if (row['id'] if row else None) != (previous['id'] if previous else None):
        raise HTTPException(status_code=409, detail='Conversation changed; please retry')


@router.post('', status_code=201)
def create_conversation(payload: ConversationCreate, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        row = db.execute('INSERT INTO conversations (owner_id,title) VALUES (?,?) RETURNING id,title,created_at',
                         (user.id, payload.title)).fetchone()
        return dict(row)


@router.get('')
def list_conversations(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        return [dict(row) for row in db.execute(
            'SELECT id,title,created_at FROM conversations WHERE owner_id = ? ORDER BY id DESC', (user.id,)
        )]


@router.get('/{conversation_id}')
def get_conversation(conversation_id: int, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        conversation = _owned(db, conversation_id, user, request)
        messages = db.execute('SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY id',
                              (conversation_id,)).fetchall()
        available = _authorized_sources(db, user.role) if any(row['source_scope'] == 'general' for row in messages) else {}
        policy_available = _policy_sources(db, user.role) if any(row['source_scope'] == 'policy' for row in messages) else None
        result = [_visible_status_message(db, row, user) if row['source_scope'] == 'application_status'
                  else _visible_message(row, available, policy_available) for row in messages]
        _owned(db, conversation_id, user, request)
        return {**dict(conversation), 'messages': result}


@router.post('/{conversation_id}/messages', status_code=201)
def add_message(conversation_id: int, payload: MessageCreate, request: Request, user: Authenticated):
    path = request.app.state.database_path
    if payload.mode == 'application_status':
        if user.role != 'candidate':
            raise HTTPException(status_code=403, detail='Candidate access required')
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            _owned(db, conversation_id, user, request)
            candidate_stage(db, payload.application_id, user.id)
            row = db.execute('''INSERT INTO conversation_messages
                (conversation_id,question,answer,sources_json,mode,context_role,source_scope,status_application_id)
                VALUES (?,?,?,'[]','application_status','candidate','application_status',?) RETURNING *''',
                (conversation_id, payload.question, '', payload.application_id)).fetchone()
            _owned(db, conversation_id, user, request)
            return _visible_status_message(db, row, user)
    # The policy specialist receives one explicit question, without prior context.
    wants_context = payload.mode == 'general' and is_context_followup(payload.question)
    with connect(path) as db:
        db.execute('BEGIN')
        _owned(db, conversation_id, user, request)
        row = db.execute('SELECT * FROM conversation_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1',
                         (conversation_id,)).fetchone()
        previous = dict(row) if row else None
        available_context = bool(wants_context and previous and _context_still_allowed(db, user, conversation_id, previous))
    limit = min(500, max(0, 1999 - len(payload.question)))
    previous_question = previous['question'][:limit] if wants_context and available_context and limit else ''
    if previous_question:
        context_notice = 'The previous question was used as context for this search; verify the sources.'
    elif previous and wants_context:
        context_notice = 'Previous context was unavailable or exceeded the limit; this search used only the current question.'
    else:
        context_notice = ''

    def authorize_sources(sources):
        with connect(path) as db:
            db.execute('BEGIN')
            _owned(db, conversation_id, user, request)
            _assert_conversation_head(db, conversation_id, previous)
            if previous_question and not _context_still_allowed(db, user, conversation_id, previous):
                raise HTTPException(status_code=409, detail='Previous context access changed; please retry')
            available = _authorized_sources(db, user.role)
            if any(available.get(source.id) != source for source in sources):
                raise HTTPException(status_code=409, detail='Source access changed; please retry')
    if payload.mode == 'policy':
        from app.policy_agent import run_policy_agent

        # The specialist revalidates its selected handbook sources; this
        # callback separately enforces owner, session and conversation order.
        try:
            result = run_policy_agent(path, user, payload.question,
                                      enabled=os.environ.get('TAWZEEF_ENABLE_GROQ') == '1',
                                      authorize=lambda: authorize_sources([]))
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail='Policy access changed; please retry') from exc
        answer = result.answer
    else:
        answer = answer_question(payload.question, user.role, database_path=path,
                                 enable_groq=os.environ.get('TAWZEEF_ENABLE_GROQ') == '1',
                                 authorize_sources=authorize_sources, previous_question=previous_question)
    source_records = [{'id': source.id, 'digest': _digest(source)} for source in answer.sources]
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        _owned(db, conversation_id, user, request)
        _assert_conversation_head(db, conversation_id, previous)
        if previous_question and not _context_still_allowed(db, user, conversation_id, previous):
            raise HTTPException(status_code=409, detail='Previous context access changed; please retry')
        available = _policy_sources(db, user.role) if payload.mode == 'policy' else _authorized_sources(db, user.role)
        if (not source_records and answer.mode != 'abstain') or any(
            source['id'] not in available or _digest(available[source['id']]) != source['digest']
            for source in source_records
        ):
            raise HTTPException(status_code=409, detail='Source access changed; please retry')
        row = db.execute('''INSERT INTO conversation_messages
            (conversation_id,question,answer,sources_json,mode,notice,context_role,source_scope) VALUES (?,?,?,?,?,?,?,?)
            RETURNING *''', (conversation_id, payload.question, answer.answer,
                              json.dumps(source_records), answer.mode,
                              ' '.join(filter(None, (answer.notice, context_notice))) or None, user.role, payload.mode)).fetchone()
        return _visible_message(row, available, available if payload.mode == 'policy' else None)
