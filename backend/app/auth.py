"""Password verification and server-side session/authorization helpers."""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.auth_store import InvalidAccount, issue_session, session_user
from app.db import connect

COOKIE_NAME = "tawzeef_session"
SESSION_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(derived, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(request: Request, user_id: int, *, expected_password_hash: str) -> str:
    token = secrets.token_urlsafe(32)
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        now = int(time.time())
        try:
            issue_session(db, dialect='sqlite', user_id=user_id, expected_password_hash=expected_password_hash,
                          token_hash=token_digest(token), now=now, expires_at=now + SESSION_SECONDS)
        except InvalidAccount:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid credentials') from None
    return token


def get_current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with connect(request.app.state.database_path) as db:
            row = session_user(db, dialect='sqlite', token_hash=token_digest(token), now=int(time.time()))
        if row:
            return User(**dict(row))
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def require_roles(*roles: str):
    def guard(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        return user

    return guard


def require_owner_or_hr(user: User, owner_id: int) -> None:
    if user.role != "hr" and not (user.role == "candidate" and user.id == owner_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
