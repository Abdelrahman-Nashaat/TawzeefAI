"""Concurrent startup must serialize the real PostgreSQL saver migrations."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, mode  # noqa: F401

pytest.importorskip('langgraph.checkpoint.postgres')
from langgraph.checkpoint.postgres import PostgresSaver

from app.main import create_app


def test_concurrent_starts_serialize_checkpoint_setup(mode, monkeypatch):  # noqa: F811
    entered = Event()
    contender_entered = Event()
    contender_acquired = Event()
    release = Event()
    lock = Lock()
    calls = []
    lock_calls = []
    original = PostgresSaver.setup
    import app.postgres_experimental as postgres_app
    original_acquire = postgres_app._acquire_schema_lock

    def observed_acquire(db, schema):
        with lock:
            lock_calls.append(db.info.backend_pid)
            contender = len(lock_calls) == 2
        if contender:
            contender_entered.set()
        original_acquire(db, schema)
        if contender:
            contender_acquired.set()

    def paused_setup(self):
        with lock:
            calls.append(self.conn.info.backend_pid)
            first = len(calls) == 1
        if first:
            entered.set()
            if not release.wait(30):
                raise TimeoutError('First migration was not released')
        return original(self)

    monkeypatch.setattr(PostgresSaver, 'setup', paused_setup)
    monkeypatch.setattr(postgres_app, '_acquire_schema_lock', observed_acquire)

    def start_and_check():
        with TestClient(create_app(seed_demo=True)) as client:
            response = client.get('/api/health')
            assert response.status_code == 200
            return response.json()['mode']

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start_and_check)
        try:
            assert entered.wait(20), 'First app did not enter checkpoint setup'
            second = pool.submit(start_and_check)
            assert contender_entered.wait(20), 'Second startup did not attempt its lock'
            # Observe the contender after an unsuccessful try-lock query while
            # the first connection holds the migration lock. It must be idle
            # outside a transaction so concurrent index creation can progress.
            deadline = time.monotonic() + 20
            with db_for(mode) as db:
                while time.monotonic() < deadline:
                    db.execute('SELECT pg_stat_clear_snapshot()')
                    waiting = db.execute('''SELECT 1 FROM pg_stat_activity
                        WHERE pid=%s AND state='idle' AND xact_start IS NULL
                        AND query LIKE 'SELECT pg_try_advisory_lock%%' ''', (lock_calls[1],)).fetchone()
                    if waiting:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail('Second app did not poll without an open transaction')
            assert not contender_acquired.is_set()
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=30) == 'postgres-experimental-recruitment'
        assert second.result(timeout=30) == 'postgres-experimental-recruitment'

    assert len(calls) == 2
    assert contender_acquired.is_set()
    with db_for(mode) as db:
        versions = [row['v'] for row in db.execute('SELECT v FROM checkpoint_migrations ORDER BY v').fetchall()]
        assert versions == list(range(len(PostgresSaver.MIGRATIONS)))
