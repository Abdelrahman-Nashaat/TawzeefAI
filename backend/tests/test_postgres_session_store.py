"""Exercise the shared session operation against an isolated PostgreSQL schema."""

import os
import threading
import uuid

import pytest

psycopg = pytest.importorskip('psycopg')
from psycopg import sql
from psycopg.rows import dict_row

from app.auth_store import InvalidAccount, issue_session, reset_password_and_revoke, session_user


@pytest.fixture
def database():
    dsn = os.environ.get('TAWZEEF_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Set TAWZEEF_TEST_POSTGRES_DSN to a disposable PostgreSQL database')
    schema = 'tawzeef_auth_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            admin.execute(sql.SQL("CREATE TABLE {}.users (id bigint PRIMARY KEY, active smallint NOT NULL, password_hash text NOT NULL, username text NOT NULL DEFAULT 'candidate', display_name text NOT NULL DEFAULT 'Candidate', role text NOT NULL DEFAULT 'candidate')").format(sql.Identifier(schema)))
            admin.execute(sql.SQL('CREATE TABLE {}.sessions (token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES {}.users(id), expires_at bigint NOT NULL)').format(sql.Identifier(schema), sql.Identifier(schema)))
            admin.execute(sql.SQL("INSERT INTO {}.users (id, active, password_hash) VALUES (1, 1, 'hash-v1')").format(sql.Identifier(schema)))
            yield dsn, schema
        finally:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def connection(database):
    dsn, schema = database
    db = psycopg.connect(dsn, row_factory=dict_row)
    db.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
    db.commit()
    return db


def issue(db, token='digest', password='hash-v1'):
    return issue_session(db, dialect='postgres', user_id=1, expected_password_hash=password,
                         token_hash=token, now=100, expires_at=200)


def test_autocommit_connection_is_rejected(database):
    dsn, _ = database
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as db, \
            pytest.raises(ValueError, match='autocommit disabled'):
        issue(db)


def test_issue_commit_and_rollback(database):
    with connection(database) as db:
        issue(db)
    with connection(database) as db:
        assert db.execute('SELECT token_hash FROM sessions').fetchone()['token_hash'] == 'digest'
        with pytest.raises(RuntimeError), db.transaction():
            issue(db, token='rolled-back')
            raise RuntimeError('rollback')
        assert db.execute("SELECT 1 FROM sessions WHERE token_hash='rolled-back'").fetchone() is None


def test_expiry_cleanup_and_insertion_are_atomic(database):
    with connection(database) as db:
        db.execute("INSERT INTO sessions VALUES ('expired',1,100), ('unexpired',1,101)")
    with connection(database) as db:
        with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
            issue(db, token='unexpired')
        assert db.execute("SELECT 1 FROM sessions WHERE token_hash='expired'").fetchone() is not None
        issue(db, token='new')
    with connection(database) as db:
        tokens = {row['token_hash'] for row in db.execute('SELECT token_hash FROM sessions')}
        assert tokens == {'unexpired', 'new'}


@pytest.mark.parametrize('update', ["UPDATE users SET active=0 WHERE id=1", "UPDATE users SET password_hash='hash-v2' WHERE id=1"])
def test_changed_account_cannot_issue_session(database, update):
    with connection(database) as db:
        db.execute(update)
    with connection(database) as db:
        with pytest.raises(InvalidAccount):
            issue(db)
        assert db.execute('SELECT count(*) AS n FROM sessions').fetchone()['n'] == 0


def test_password_reset_serializes_and_revokes_new_session(database):
    started = threading.Event()
    finished = threading.Event()
    errors = []

    def reset():
        try:
            with connection(database) as db:
                started.set()
                reset_password_and_revoke(db, dialect='postgres', user_id=1, password_hash='hash-v2')
        except Exception as error:  # noqa: BLE001 - surface failures from the worker thread
            errors.append(error)
        finally:
            finished.set()

    with connection(database) as first:
        issue(first)
        thread = threading.Thread(target=reset, daemon=True)
        thread.start()
        assert started.wait(10)
        assert not finished.wait(0.2)
        first.commit()
        thread.join(10)
        assert not thread.is_alive() and not errors
    with connection(database) as db:
        assert db.execute('SELECT count(*) AS n FROM sessions').fetchone()['n'] == 0
        with pytest.raises(InvalidAccount):
            issue(db)


def test_reset_locked_first_causes_waiting_issuance_to_recheck_account(database):
    started = threading.Event()
    finished = threading.Event()
    errors = []
    outcomes = []

    def attempt_issue():
        try:
            with connection(database) as db:
                started.set()
                try:
                    issue(db, token='waiting-session')
                except InvalidAccount:
                    outcomes.append('invalid')
                else:
                    outcomes.append('issued')
        except Exception as error:  # noqa: BLE001 - surface failures from the worker thread
            errors.append(error)
        finally:
            finished.set()

    with connection(database) as first:
        reset_password_and_revoke(first, dialect='postgres', user_id=1, password_hash='hash-v2')
        thread = threading.Thread(target=attempt_issue, daemon=True)
        thread.start()
        assert started.wait(10)
        assert not finished.wait(0.2)
        first.commit()
        thread.join(10)
        assert not thread.is_alive() and not errors
    assert outcomes == ['invalid']
    with connection(database) as db:
        assert db.execute('SELECT count(*) AS n FROM sessions').fetchone()['n'] == 0


def test_session_lookup_enforces_expiry_active_and_role(database):
    with connection(database) as db:
        issue(db)
    with connection(database) as db:
        row = session_user(db, dialect='postgres', token_hash='digest', now=199)
        assert (row['id'], row['role']) == (1, 'candidate')
        assert session_user(db, dialect='postgres', token_hash='digest', now=200) is None
        db.execute("UPDATE users SET role='employee' WHERE id=1")
        assert session_user(db, dialect='postgres', token_hash='digest', now=199)['role'] == 'employee'
        db.execute('UPDATE users SET active=0 WHERE id=1')
        assert session_user(db, dialect='postgres', token_hash='digest', now=199) is None


def test_reset_and_revocation_rollback_together(database):
    with connection(database) as db:
        issue(db)
    with connection(database) as db:
        with pytest.raises(RuntimeError), db.transaction():
            reset_password_and_revoke(db, dialect='postgres', user_id=1, password_hash='hash-v2')
            raise RuntimeError('force rollback')
        assert db.execute('SELECT password_hash FROM users WHERE id=1').fetchone()['password_hash'] == 'hash-v1'
        assert session_user(db, dialect='postgres', token_hash='digest', now=199) is not None
        reset_password_and_revoke(db, dialect='postgres', user_id=1, password_hash='hash-v2')
        assert session_user(db, dialect='postgres', token_hash='digest', now=199) is None
