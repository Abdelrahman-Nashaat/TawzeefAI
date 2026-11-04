"""Local administrative capability, separate from recruitment access roles."""

import argparse
import getpass
import json
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.auth import COOKIE_NAME, User, get_current_user, hash_password, token_digest
from app.auth_store import reset_password_and_revoke
from app.db import connect, initialize

router = APIRouter(prefix='/api/admin')
Authenticated = Annotated[User, Depends(get_current_user)]
Role = Literal['candidate', 'employee', 'hr']


def initialize_admin_users(path: str | Path) -> None:
    """Add capability and active flag without rebuilding referenced users."""
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
        if 'active' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))')
        db.execute('''CREATE TABLE IF NOT EXISTS user_admins (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            granted_by INTEGER REFERENCES users(id),
            granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS user_admin_audit (
            id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id),
            target_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )''')


class UserCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.-]+$')
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=256)
    role: Role
    is_admin: StrictBool = False

    @field_validator('display_name')
    @classmethod
    def valid_name(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or not value.strip():
            raise ValueError('Display name must contain printable text')
        return value.strip()


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Role | None = None
    active: StrictBool | None = None
    is_admin: StrictBool | None = None

    @model_validator(mode='after')
    def require_change(self):
        if self.role is None and self.active is None and self.is_admin is None:
            raise ValueError('At least one change is required')
        return self


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=12, max_length=256)


def _actor(db, request: Request, user: User) -> None:
    """Recheck session and admin status within the current DB transaction."""
    token = request.cookies.get(COOKIE_NAME, '')
    row = db.execute('''SELECT 1 FROM users u
        JOIN user_admins a ON a.user_id=u.id JOIN sessions s ON s.user_id=u.id
        WHERE u.id=? AND u.active=1 AND s.token_hash=? AND s.expires_at>?''',
        (user.id, token_digest(token), int(time.time()))).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail='Administrator access required')


def _audit(db, actor_id: int | None, target_id: int, action: str, details: dict) -> None:
    db.execute('INSERT INTO user_admin_audit (actor_id,target_id,action,details) VALUES (?,?,?,?)',
               (actor_id, target_id, action, json.dumps(details, ensure_ascii=False)))


def _user(db, user_id: int) -> dict:
    row = db.execute('''SELECT u.id,u.username,u.display_name,u.role,u.active,
        EXISTS(SELECT 1 FROM user_admins a WHERE a.user_id=u.id) AS is_admin
        FROM users u WHERE u.id=?''', (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='User not found')
    result = dict(row)
    result['active'] = bool(result['active'])
    result['is_admin'] = bool(result['is_admin'])
    return result


def bootstrap_admin(path: str | Path, username: str, display_name: str, password: str) -> int:
    """One-time local bootstrap creating a new employee with admin capability.

    This is deliberately not an HTTP endpoint. Refuses if any admin exists.
    """
    payload = UserCreate(username=username, display_name=display_name, password=password,
                         role='employee', is_admin=True)
    initialize(path)
    initialize_admin_users(path)
    password_hash = hash_password(payload.password)
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM user_admins LIMIT 1').fetchone():
            raise ValueError('An administrator already exists')
        if db.execute('SELECT 1 FROM users WHERE username=?', (payload.username,)).fetchone():
            raise ValueError('Username already exists')
        user_id = db.execute('INSERT INTO users(username,display_name,role,password_hash) VALUES (?,?,?,?)',
                             (payload.username, payload.display_name, payload.role, password_hash)).lastrowid
        db.execute('INSERT INTO user_admins(user_id,granted_by) VALUES (?,NULL)', (user_id,))
        _audit(db, None, user_id, 'bootstrap', {'role': payload.role})
        return user_id


@router.get('/access')
def access(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        return {'is_admin': bool(db.execute('SELECT 1 FROM user_admins WHERE user_id=?', (user.id,)).fetchone())}


@router.get('/users')
def list_users(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _actor(db, request, user)
        ids = [row['id'] for row in db.execute('SELECT id FROM users ORDER BY id')]
        return [_user(db, user_id) for user_id in ids]


@router.get('/audit')
def list_admin_audit(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _actor(db, request, user)
        return [dict(row) for row in db.execute(
            'SELECT id,actor_id,target_id,action,details,created_at FROM user_admin_audit ORDER BY id DESC LIMIT 500')]


@router.post('/users', status_code=201)
def create_user(payload: UserCreate, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        _actor(db, request, user)
    password_hash = hash_password(payload.password)
    try:
        with connect(request.app.state.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            _actor(db, request, user)
            if db.execute('SELECT 1 FROM users WHERE username=?', (payload.username,)).fetchone():
                raise HTTPException(status_code=409, detail='Username already exists')
            user_id = db.execute('INSERT INTO users(username,display_name,role,password_hash) VALUES (?,?,?,?)',
                                 (payload.username, payload.display_name, payload.role, password_hash)).lastrowid
            if payload.is_admin:
                db.execute('INSERT INTO user_admins(user_id,granted_by) VALUES (?,?)', (user_id, user.id))
            _audit(db, user.id, user_id, 'created', {'role': payload.role, 'is_admin': payload.is_admin})
            return _user(db, user_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail='Could not create user') from exc


@router.patch('/users/{user_id}')
def update_user(user_id: int, payload: UserUpdate, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _actor(db, request, user)
        current = _user(db, user_id)
        role = current['role'] if payload.role is None else payload.role
        active = current['active'] if payload.active is None else payload.active
        is_admin = current['is_admin'] if payload.is_admin is None else payload.is_admin
        if current['active'] and current['is_admin'] and (not active or not is_admin):
            count = db.execute('SELECT COUNT(*) FROM user_admins a JOIN users u ON u.id=a.user_id WHERE u.active=1').fetchone()[0]
            if count <= 1:
                raise HTTPException(status_code=409, detail='Cannot remove the last active administrator')
        changed = role != current['role'] or active != current['active'] or is_admin != current['is_admin']
        if changed:
            db.execute('UPDATE users SET role=?,active=? WHERE id=?', (role, int(active), user_id))
            if is_admin and not current['is_admin']:
                db.execute('INSERT INTO user_admins(user_id,granted_by) VALUES (?,?)', (user_id, user.id))
            elif not is_admin and current['is_admin']:
                db.execute('DELETE FROM user_admins WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            _audit(db, user.id, user_id, 'updated', {'role': role, 'active': active, 'is_admin': is_admin})
        return _user(db, user_id)


@router.post('/users/{user_id}/password')
def reset_password(user_id: int, payload: PasswordReset, request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        _actor(db, request, user)
    password_hash = hash_password(payload.password)
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _actor(db, request, user)
        _user(db, user_id)
        reset_password_and_revoke(db, dialect='sqlite', user_id=user_id, password_hash=password_hash)
        _audit(db, user.id, user_id, 'password_reset', {})
        return {'status': 'updated'}


def main():
    parser = argparse.ArgumentParser(description='Create the first local TawzeefAI administrator')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--display-name', required=True)
    args = parser.parse_args()
    password = getpass.getpass('New password: ')
    if password != getpass.getpass('Repeat password: '):
        raise SystemExit('Passwords do not match')
    try:
        bootstrap_admin(args.database, args.username, args.display_name, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print('Administrator created; sign in through the local application.')


if __name__ == '__main__':
    main()
