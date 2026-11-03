"""Real PostgreSQL tests for the deliberately partial offline copy."""

import os
import sqlite3
import subprocess
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

psycopg = pytest.importorskip('psycopg')
from psycopg import sql

from app import postgres_core_rehearsal as migration
from app.db import connect
from app.main import create_app
from app.postgres_core_rehearsal import TABLE_COLUMNS, rehearse


@pytest.fixture
def pg():
    dsn = os.environ.get('TAWZEEF_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Set TAWZEEF_TEST_POSTGRES_DSN to a disposable PostgreSQL database')
    schema = 'tawzeef_rehearsal_test_' + uuid.uuid4().hex[:20]
    with psycopg.connect(dsn, autocommit=True) as connection:
        try:
            yield connection, schema
        finally:
            # Only the randomized, explicitly created test namespace is removed.
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def source(tmp_path, monkeypatch):
    for name in ('TAWZEEF_ENABLE_GROQ', 'TAWZEEF_ENABLE_GOOGLE', 'TAWZEEF_AUTO_PROVISIONAL'):
        monkeypatch.setenv(name, '0')
    path = tmp_path / 'synthetic.sqlite3'
    with TestClient(create_app(path, seed_demo=True)), connect(path) as db:
        candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
        hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
        app = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text,score_json)
            VALUES (?,'job-backend-python','fictional.docx','Skills: Python, SQL','{"score": 42}')''', (candidate,)).lastrowid
        db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,'submitted','{}')", (app, hr))
    return path


def table_rows(connection, schema, table):
    columns = TABLE_COLUMNS[table]
    query = sql.SQL('SELECT {} FROM {}.{} ORDER BY id').format(
        sql.SQL(', ').join(map(sql.Identifier, columns)), sql.Identifier(schema), sql.Identifier(table))
    return connection.execute(query).fetchall()


def test_copy_preserves_values_foreign_keys_and_counts(pg, source):
    connection, schema = pg
    counts = rehearse(source, connection, schema)
    with sqlite3.connect(source) as original:
        for table, columns in TABLE_COLUMNS.items():
            names = ', '.join(f'"{name}"' for name in columns)
            rows = original.execute(f'SELECT {names} FROM "{table}" ORDER BY id').fetchall()
            assert table_rows(connection, schema, table) == rows
            assert counts[table] == len(rows)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(sql.SQL("INSERT INTO {}.audit_events VALUES (9999,9999,9999,'x','{{}}','x')").format(sql.Identifier(schema)))


def test_bad_source_rolls_back_entire_schema(pg, source):
    connection, schema = pg
    with sqlite3.connect(source) as db:
        db.execute("UPDATE applications SET resume_text='bad' || char(0) || 'value'")
    with pytest.raises(ValueError, match='Unsupported text'):
        rehearse(source, connection, schema)
    assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone() is None


def test_existing_destination_and_unknown_source_column_are_never_overwritten(pg, source):
    connection, schema = pg
    connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    connection.execute(sql.SQL('CREATE TABLE {}.sentinel(value text)').format(sql.Identifier(schema)))
    with pytest.raises(psycopg.errors.DuplicateSchema):
        rehearse(source, connection, schema)
    assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone()
    with sqlite3.connect(source) as db:
        db.execute('ALTER TABLE users ADD COLUMN unsupported TEXT')
    with pytest.raises(ValueError, match='Unsupported source schema'):
        rehearse(source, connection, schema + 'a')
    assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema + 'a',)).fetchone() is None


def test_copy_reads_one_snapshot_while_source_changes(pg, source, monkeypatch):
    connection, schema = pg
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA journal_mode=WAL')
        before = db.execute("SELECT title FROM jobs WHERE id='job-backend-python'").fetchone()[0]
    original = migration._snapshot

    def snapshot_then_change(path):
        snapshot = original(path)
        with sqlite3.connect(source) as writer:
            writer.execute("UPDATE jobs SET title='Changed after snapshot' WHERE id='job-backend-python'")
        return snapshot

    monkeypatch.setattr(migration, '_snapshot', snapshot_then_change)
    rehearse(source, connection, schema)
    title = connection.execute(sql.SQL("SELECT title FROM {}.jobs WHERE id='job-backend-python'").format(sql.Identifier(schema))).fetchone()[0]
    assert title == before
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT title FROM jobs WHERE id='job-backend-python'").fetchone()[0] == 'Changed after snapshot'


def test_rejects_unsafe_schema_without_touching_database(pg, source):
    connection, _ = pg
    for name in ('public', 'tawzeef_rehearsal_', 'tawzeef_rehearsal_test;DROP', 'x'):
        with pytest.raises(ValueError, match='prefix'):
            rehearse(source, connection, name)


def test_rejects_existing_transaction_and_source_fk_corruption(pg, source):
    connection, schema = pg
    with connection.transaction(), pytest.raises(ValueError, match='idle'):
        rehearse(source, connection, schema)
    assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone() is None
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('UPDATE applications SET candidate_id=999999')
    with pytest.raises(ValueError, match='foreign-key'):
        rehearse(source, connection, schema)
    assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone() is None


def test_cli_reports_only_counts_and_generic_failure(pg, source):
    connection, schema = pg
    env = os.environ.copy()
    env['TAWZEEF_POSTGRES_REHEARSAL_DSN'] = env['TAWZEEF_TEST_POSTGRES_DSN']
    command = [sys.executable, '-m', 'app.postgres_core_rehearsal', '--source', str(source), '--schema', schema]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0
    assert 'partial_rehearsal_only' in result.stdout and 'sensitive data' in result.stdout
    assert 'synthetic-test-only' not in result.stdout + result.stderr
    assert 'Skills: Python' not in result.stdout + result.stderr
    failure = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert failure.returncode == 1
    assert 'no new schema was committed' in failure.stderr
    assert 'synthetic-test-only' not in failure.stdout + failure.stderr
    assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone()
