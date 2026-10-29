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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.auth import COOKIE_NAME, User, get_current_user, token_digest
from app.db import connect
from app.knowledge import (
    DEFAULT_FIXTURES,
    ROLE_AUDIENCES,
    Source,
    _document_passages,
    _documents,
    answer_question,
)

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
    for row in db.execute('SELECT id,title,audience,text,pages_json FROM knowledge_documents WHERE active = 1'):
        if row['audience'] in allowed:
            sources.extend(_document_passages(dict(row)))
    # Ambiguous IDs must never authorize a stale stored response.
    by_id = {source.id: source for source in sources}
    if len(by_id) != len(sources):
        return {}
    return by_id


def _visible_message(row, available: dict) -> dict:
    try:
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


def _digest(source: Source) -> str:
    data = asdict(source)
    if data['page'] is None:
        # Preserve digests for historical messages written before page metadata.
        del data['page']
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


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
        available = _authorized_sources(db, user.role)
        messages = db.execute('SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY id',
                              (conversation_id,)).fetchall()
        return {**dict(conversation), 'messages': [_visible_message(row, available) for row in messages]}


@router.post('/{conversation_id}/messages', status_code=201)
def add_message(conversation_id: int, payload: MessageCreate, request: Request, user: Authenticated):
    path = request.app.state.database_path
    with connect(path) as db:
        _owned(db, conversation_id, user, request)
    answer = answer_question(payload.question, user.role, database_path=path,
                             enable_groq=os.environ.get('TAWZEEF_ENABLE_GROQ') == '1')
    source_records = [{'id': source.id, 'digest': _digest(source)} for source in answer.sources]
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        _owned(db, conversation_id, user, request)
        available = _authorized_sources(db, user.role)
        if (not source_records and answer.mode != 'abstain') or any(
            source['id'] not in available or _digest(available[source['id']]) != source['digest']
            for source in source_records
        ):
            raise HTTPException(status_code=409, detail='Source access changed; please retry')
        row = db.execute('''INSERT INTO conversation_messages
            (conversation_id,question,answer,sources_json,mode,notice) VALUES (?,?,?,?,?,?)
            RETURNING *''', (conversation_id, payload.question, answer.answer,
                              json.dumps(source_records), answer.mode, answer.notice)).fetchone()
        return _visible_message(row, available)
