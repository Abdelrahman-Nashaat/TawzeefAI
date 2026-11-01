"""SQLite connection and schema for the local development server."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(path: str | Path):
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('candidate', 'employee', 'hr')),
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                location TEXT NOT NULL,
                required_skills TEXT NOT NULL,
                preferred_skills TEXT NOT NULL,
                responsibilities TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                candidate_id INTEGER NOT NULL REFERENCES users(id),
                job_id TEXT NOT NULL REFERENCES jobs(id),
                filename TEXT NOT NULL,
                resume_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                score_json TEXT
            );
            CREATE INDEX IF NOT EXISTS applications_owner ON applications(candidate_id);
            CREATE TABLE IF NOT EXISTS job_offers (
                id INTEGER PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                details TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('presented', 'accepted', 'declined', 'withdrawn')),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                responded_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS job_offer_current ON job_offers(application_id)
                WHERE status IN ('presented', 'accepted');
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                actor_id INTEGER NOT NULL REFERENCES users(id),
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS screening_history (
                audit_event_id INTEGER PRIMARY KEY REFERENCES audit_events(id),
                score_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS screened_history_application
                ON audit_events(application_id, action, id DESC);
            """
        )
