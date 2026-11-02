"""Opt-in local provisional screening with durable, fenced SQLite work claims."""

import secrets
import sqlite3
import time
from pathlib import Path
from threading import Event, Thread

from fastapi import HTTPException

from app.db import connect
from app.provisional_queue import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_CHARS,
    _assignment_revision,
    _bounded_job,
    _job_revision,
    _latest,
    _source_revisions,
    score_masked_resume,
)
from app.resume_mask import masked_source

LEASE_SECONDS = 60
MAX_ATTEMPTS = 3
MAX_CONCURRENT_CLAIMS = 2


def initialize_auto_screen(path: str | Path):
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS auto_provisional_jobs (
            snapshot_id INTEGER PRIMARY KEY REFERENCES provisional_snapshots(id),
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','manual','done','superseded','failed')),
            attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL,
            job_id TEXT, job_revision INTEGER, assignment_revision INTEGER,
            source_revision INTEGER, mask_revision INTEGER, source_review_status TEXT,
            automatic_masked INTEGER, additional_masked INTEGER, score INTEGER,
            required_mentioned INTEGER, required_total INTEGER,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS auto_provisional_state ON auto_provisional_jobs(state, snapshot_id);
        CREATE TRIGGER IF NOT EXISTS auto_provisional_enqueue AFTER INSERT ON provisional_snapshots
        WHEN NEW.snapshot_kind='pending' BEGIN
            INSERT OR IGNORE INTO auto_provisional_jobs(snapshot_id) VALUES(NEW.id);
        END;
        CREATE TABLE IF NOT EXISTS auto_provisional_backfill (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), after_id INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO auto_provisional_backfill(singleton,after_id) VALUES(1,0);
        CREATE TABLE IF NOT EXISTS auto_provisional_workers (
            worker_id TEXT PRIMARY KEY, last_heartbeat REAL NOT NULL
        );
        ''')


def claim(path: str | Path):
    now = time.time()
    token = secrets.token_hex(24)
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        cursor = db.execute('SELECT after_id FROM auto_provisional_backfill WHERE singleton=1').fetchone()[0]
        batch = db.execute('SELECT id,application_id,snapshot_kind FROM provisional_snapshots WHERE id>? ORDER BY id LIMIT 100', (cursor,)).fetchall()
        for item in batch:
            if item['snapshot_kind'] == 'pending':
                latest = _latest(db, item['application_id'])
                if latest and latest['id'] == item['id']:
                    db.execute('INSERT OR IGNORE INTO auto_provisional_jobs(snapshot_id) VALUES(?)', (item['id'],))
        if batch:
            db.execute('UPDATE auto_provisional_backfill SET after_id=? WHERE singleton=1', (batch[-1]['id'],))
        db.execute("UPDATE auto_provisional_jobs SET state='failed',lease_token=NULL,lease_until=NULL WHERE state='running' AND lease_until<? AND attempts>=?", (now, MAX_ATTEMPTS))
        running = db.execute("SELECT count(*) FROM auto_provisional_jobs WHERE state='running' AND lease_until>?", (now,)).fetchone()[0]
        if running >= MAX_CONCURRENT_CLAIMS:
            return None
        row = db.execute('''SELECT snapshot_id FROM auto_provisional_jobs
            WHERE state='pending' OR (state='running' AND lease_until<?)
            ORDER BY snapshot_id LIMIT 1''', (now,)).fetchone()
        if row is None:
            return None
        db.execute('''UPDATE auto_provisional_jobs SET state='running',attempts=attempts+1,
                    lease_token=?,lease_until=? WHERE snapshot_id=?''',
                   (token, now + LEASE_SECONDS, row['snapshot_id']))
        return row['snapshot_id'], token


def _finish(path, snapshot_id, token, state):
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''UPDATE auto_provisional_jobs SET state=?,lease_token=NULL,lease_until=NULL
            WHERE snapshot_id=? AND state='running' AND lease_token=? AND lease_until>?''',
            (state, snapshot_id, token, time.time()))


def process_claim(path: str | Path, snapshot_id: int, token: str):
    try:
        with connect(path) as db:
            db.execute('BEGIN')
            row = db.execute('''SELECT s.application_id,s.job_id,s.snapshot_kind,
                a.status,length(CAST(a.resume_text AS BLOB)) AS source_bytes,a.job_id AS current_job
                FROM provisional_snapshots s JOIN applications a ON a.id=s.application_id WHERE s.id=?''',
                (snapshot_id,)).fetchone()
            if row is None:
                return
            application_id = row['application_id']
            latest = _latest(db, application_id)
            if (row['snapshot_kind'] != 'pending' or latest['id'] != snapshot_id or
                    row['status'] in ('hired', 'rejected') or row['current_job'] != row['job_id']):
                outcome = 'superseded'
            elif row['source_bytes'] > MAX_SOURCE_BYTES:
                outcome = 'manual'
            else:
                text = db.execute('SELECT resume_text FROM applications WHERE id=?', (application_id,)).fetchone()[0]
                outcome = 'manual' if len(text) > MAX_SOURCE_CHARS else None
            if outcome is None:
                job = _bounded_job(db, row['job_id'])
                source = masked_source(db, application_id)
                source_revisions = _source_revisions(db, application_id)
                job_revision = _job_revision(db, row['job_id'])
                assignment_revision = _assignment_revision(db, application_id)
                job_id = row['job_id']
        if outcome:
            _finish(path, snapshot_id, token, outcome)
            return
        result = score_masked_resume(job, source)
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            lease = db.execute('SELECT state,lease_token,lease_until FROM auto_provisional_jobs WHERE snapshot_id=?',
                               (snapshot_id,)).fetchone()
            if lease is None or lease['state'] != 'running' or lease['lease_token'] != token or lease['lease_until'] <= time.time():
                return
            application = db.execute('SELECT job_id,status FROM applications WHERE id=?', (application_id,)).fetchone()
            latest = _latest(db, application_id)
            if (application is None or application['status'] in ('hired','rejected') or application['job_id'] != job_id
                    or latest is None or latest['id'] != snapshot_id or _job_revision(db, job_id) != job_revision
                    or _assignment_revision(db, application_id) != assignment_revision
                    or _source_revisions(db, application_id) != source_revisions):
                db.execute("UPDATE auto_provisional_jobs SET state='superseded',lease_token=NULL,lease_until=NULL WHERE snapshot_id=?", (snapshot_id,))
                return
            required = result['required']
            db.execute('''UPDATE auto_provisional_jobs SET state='done',lease_token=NULL,lease_until=NULL,
                job_id=?,job_revision=?,assignment_revision=?,source_revision=?,mask_revision=?,source_review_status=?,
                automatic_masked=?,additional_masked=?,score=?,required_mentioned=?,required_total=?,
                completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE snapshot_id=?''',
                (job_id,job_revision,assignment_revision,*source_revisions,source['status'],
                 int(source['automatic_masked']),int(source['additional_masked']),result['score'],
                 sum(item['mentioned'] for item in required),len(required),snapshot_id))
    except HTTPException:
        _finish(path, snapshot_id, token, 'manual')
    except Exception:  # noqa: BLE001 - persist bounded failure without logging resume data
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''UPDATE auto_provisional_jobs SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,
                lease_token=NULL,lease_until=NULL WHERE snapshot_id=? AND state='running' AND lease_token=? AND lease_until>?''',
                (MAX_ATTEMPTS,snapshot_id,token,time.time()))


def run_one(path: str | Path):
    work = claim(path)
    if work is None:
        return False
    process_claim(path, *work)
    return True


class AutoScreenWorker:
    def __init__(self, path: str | Path):
        self.path = path
        self.stop_event = Event()
        self.worker_id = secrets.token_hex(16)
        self.thread = Thread(target=self._run, name='provisional-screen-worker', daemon=False)

    def _run(self):
        next_heartbeat = 0.0
        try:
            while not self.stop_event.is_set():
                try:
                    now = time.time()
                    if now >= next_heartbeat:
                        with connect(self.path) as db:
                            db.execute('DELETE FROM auto_provisional_workers WHERE last_heartbeat<?', (now-300,))
                            db.execute('INSERT OR REPLACE INTO auto_provisional_workers(worker_id,last_heartbeat) VALUES(?,?)',
                                       (self.worker_id, now))
                        next_heartbeat = now + 5
                    did_work = run_one(self.path)
                except sqlite3.Error:
                    did_work = False
                if not did_work:
                    self.stop_event.wait(1)
        finally:
            try:
                with connect(self.path) as db:
                    db.execute('DELETE FROM auto_provisional_workers WHERE worker_id=?', (self.worker_id,))
            except sqlite3.Error:
                pass  # A stale heartbeat naturally expires if shutdown cannot write.

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()
