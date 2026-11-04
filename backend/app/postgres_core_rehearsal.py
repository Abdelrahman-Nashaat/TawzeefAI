"""Copy core tables and optionally selected lifecycle tables to PostgreSQL.

This is an offline, deliberately incomplete migration rehearsal. It never switches
the application database and is not suitable for a production cutover. The copied
tables contain password hashes and résumé text; use synthetic data only.
"""

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

SCHEMA_PREFIX = 'tawzeef_rehearsal_'
_SCHEMA = re.compile(r'^tawzeef_rehearsal_[a-z0-9_]{1,36}$')
TABLE_COLUMNS = {
    'users': ('id', 'username', 'display_name', 'role', 'password_hash', 'active'),
    'jobs': ('id', 'title', 'location', 'required_skills', 'preferred_skills', 'responsibilities'),
    'applications': ('id', 'candidate_id', 'job_id', 'filename', 'resume_text', 'status',
                     'created_at', 'updated_at', 'score_json', 'extraction_json'),
    'audit_events': ('id', 'application_id', 'actor_id', 'action', 'details', 'created_at'),
}
INTEGER_COLUMNS = {
    'users': {'id', 'active'}, 'jobs': set(),
    'applications': {'id', 'candidate_id'}, 'audit_events': {'id', 'application_id', 'actor_id'},
}
NULLABLE_COLUMNS = {'applications': {'score_json', 'extraction_json'}}
_PRIMARY_KEYS = {'users': 'id', 'jobs': 'id', 'applications': 'id', 'audit_events': 'id'}
# This optional extension deliberately excludes sessions and all other tables.
LIFECYCLE_COLUMNS = {
    'interviews': ('id', 'application_id', 'candidate_id', 'start_at', 'end_at', 'interviewer', 'interviewer_key', 'status', 'created_by', 'created_at'),
    'interview_completions': ('interview_id', 'recorded_by', 'recorded_at'),
    'interview_slots': ('id', 'start_at', 'end_at', 'interviewer', 'interviewer_key', 'created_by', 'interview_id', 'withdrawn', 'created_at', 'target_application_id', 'target_candidate_id'),
    'job_offers': ('id', 'application_id', 'details', 'status', 'created_by', 'created_at', 'responded_at'),
    'hr_notifications': ('id', 'application_id', 'submitted_job_id', 'created_at'),
    'hr_notification_reads': ('notification_id', 'actor_id', 'read_at'),
    'slot_audit_events': ('id', 'slot_id', 'actor_id', 'action', 'created_at'),
}
LIFECYCLE_KEYS = {
    'interviews': ('id',), 'interview_completions': ('interview_id',),
    'interview_slots': ('id',), 'job_offers': ('id',), 'hr_notifications': ('id',),
    'hr_notification_reads': ('notification_id', 'actor_id'), 'slot_audit_events': ('id',),
}
LIFECYCLE_INTEGERS = {
    'interviews': {'id', 'application_id', 'candidate_id', 'created_by'},
    'interview_completions': {'interview_id', 'recorded_by'},
    'interview_slots': {'id', 'created_by', 'interview_id', 'withdrawn', 'target_application_id', 'target_candidate_id'},
    'job_offers': {'id', 'application_id', 'created_by'},
    'hr_notifications': {'id', 'application_id'},
    'hr_notification_reads': {'notification_id', 'actor_id'},
    'slot_audit_events': {'id', 'slot_id', 'actor_id'},
}
LIFECYCLE_NULLABLE = {'interview_slots': {'interview_id', 'target_application_id', 'target_candidate_id'},
                      'job_offers': {'responded_at'}}
_LIFECYCLE_DDL = (
    "CREATE TABLE interviews (id bigint PRIMARY KEY, application_id bigint NOT NULL REFERENCES applications(id), candidate_id bigint NOT NULL REFERENCES users(id), start_at text NOT NULL, end_at text NOT NULL, interviewer text NOT NULL, interviewer_key text NOT NULL, status text NOT NULL CHECK (status IN ('scheduled','cancelled')), created_by bigint NOT NULL REFERENCES users(id), created_at text NOT NULL)",
    'CREATE TABLE interview_completions (interview_id bigint PRIMARY KEY REFERENCES interviews(id), recorded_by bigint NOT NULL REFERENCES users(id), recorded_at text NOT NULL)',
    'CREATE TABLE interview_slots (id bigint PRIMARY KEY, start_at text NOT NULL, end_at text NOT NULL, interviewer text NOT NULL, interviewer_key text NOT NULL, created_by bigint NOT NULL REFERENCES users(id), interview_id bigint UNIQUE REFERENCES interviews(id), withdrawn integer NOT NULL CHECK (withdrawn IN (0,1)), created_at text NOT NULL, target_application_id bigint REFERENCES applications(id), target_candidate_id bigint REFERENCES users(id))',
    "CREATE TABLE job_offers (id bigint PRIMARY KEY, application_id bigint NOT NULL REFERENCES applications(id), details text NOT NULL, status text NOT NULL CHECK (status IN ('presented','accepted','declined','withdrawn')), created_by bigint NOT NULL REFERENCES users(id), created_at text NOT NULL, responded_at text)",
    'CREATE TABLE hr_notifications (id bigint PRIMARY KEY, application_id bigint NOT NULL UNIQUE REFERENCES applications(id), submitted_job_id text NOT NULL, created_at text NOT NULL)',
    'CREATE TABLE hr_notification_reads (notification_id bigint NOT NULL REFERENCES hr_notifications(id), actor_id bigint NOT NULL REFERENCES users(id), read_at text NOT NULL, PRIMARY KEY (notification_id, actor_id))',
    'CREATE TABLE slot_audit_events (id bigint PRIMARY KEY, slot_id bigint NOT NULL REFERENCES interview_slots(id), actor_id bigint NOT NULL REFERENCES users(id), action text NOT NULL, created_at text NOT NULL)',
    "CREATE UNIQUE INDEX job_offer_current ON job_offers(application_id) WHERE status IN ('presented','accepted')",
    'CREATE INDEX interviews_application ON interviews(application_id)',
    'CREATE INDEX slots_target_application ON interview_slots(target_application_id)',
    'CREATE INDEX slot_audit_events_slot ON slot_audit_events(slot_id)',
)

_DDL = (
    '''CREATE TABLE users (
        id bigint PRIMARY KEY, username text NOT NULL UNIQUE, display_name text NOT NULL,
        role text NOT NULL CHECK (role IN ('candidate','employee','hr')),
        password_hash text NOT NULL, active smallint NOT NULL CHECK (active IN (0,1)))''',
    '''CREATE TABLE jobs (
        id text PRIMARY KEY, title text NOT NULL, location text NOT NULL,
        required_skills text NOT NULL, preferred_skills text NOT NULL, responsibilities text NOT NULL)''',
    '''CREATE TABLE applications (
        id bigint PRIMARY KEY, candidate_id bigint NOT NULL REFERENCES users(id),
        job_id text NOT NULL REFERENCES jobs(id), filename text NOT NULL, resume_text text NOT NULL,
        status text NOT NULL, created_at text NOT NULL, updated_at text NOT NULL,
        score_json text, extraction_json text)''',
    '''CREATE TABLE audit_events (
        id bigint PRIMARY KEY, application_id bigint NOT NULL REFERENCES applications(id),
        actor_id bigint NOT NULL REFERENCES users(id), action text NOT NULL,
        details text NOT NULL, created_at text NOT NULL)''',
    'CREATE INDEX applications_owner ON applications(candidate_id)',
    'CREATE INDEX audit_events_application ON audit_events(application_id)',
)


def _snapshot(source):
    """Open an existing source read-only; caller closes and ends its read snapshot."""
    path = Path(source).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for table, expected in TABLE_COLUMNS.items():
            columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            if tuple(row[1] for row in columns) != expected:
                raise ValueError(f'Unsupported source schema for {table}; no data copied')
            for row in columns:
                name, declared_type, not_null, primary_key = row[1], row[2].upper(), row[3], row[5]
                expected_type = 'INTEGER' if name in INTEGER_COLUMNS[table] else 'TEXT'
                expected_pk = int(name == _PRIMARY_KEYS[table])
                expected_not_null = int(not expected_pk and name not in NULLABLE_COLUMNS.get(table, set()))
                if (declared_type, not_null, primary_key) != (expected_type, expected_not_null, expected_pk):
                    raise ValueError(f'Unsupported source schema for {table}; no data copied')
        # Start a stable snapshot before returning and fail on FK corruption.
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('Source database has a foreign-key violation; no data copied')
        return db
    except Exception:
        db.close()
        raise


def _validate(table, row):
    for name, value in zip(TABLE_COLUMNS[table], row, strict=True):
        if value is None:
            if name not in NULLABLE_COLUMNS.get(table, set()):
                raise ValueError(f'Invalid null in {table}.{name}; no data copied')
        elif name in INTEGER_COLUMNS[table]:
            if type(value) is not int:
                raise ValueError(f'Invalid integer in {table}.{name}; no data copied')
        elif not isinstance(value, str) or '\x00' in value:
            raise ValueError(f'Unsupported text in {table}.{name}; no data copied')


def _verify_rows(source_db, connection, table, columns, keys=('id',)):
    """Compare every stored value without printing source data."""
    sqlite_columns = ', '.join(f'"{name}"' for name in columns)
    sqlite_order = ', '.join(f'"{key}"' for key in keys)
    original = source_db.execute(f'SELECT {sqlite_columns} FROM "{table}" ORDER BY {sqlite_order}')
    ordering = sql.SQL('id COLLATE "C"') if table == 'jobs' else sql.SQL(', ').join(map(sql.Identifier, keys))
    query = sql.SQL('SELECT {} FROM {} ORDER BY {}').format(
        sql.SQL(', ').join(map(sql.Identifier, columns)), sql.Identifier(table), ordering)
    with connection.cursor(name=f'rehearsal_verify_{table}') as target:
        target.execute(query)
        while True:
            source_rows, target_rows = original.fetchmany(100), target.fetchmany(100)
            if source_rows != target_rows:
                raise RuntimeError(f'Value verification failed for {table}; no data copied')
            if not source_rows:
                return


def _validate_lifecycle_schema(db):
    for table, columns in LIFECYCLE_COLUMNS.items():
        actual = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        if tuple(row[1] for row in actual) != columns:
            raise ValueError(f'Unsupported source schema for {table}; no data copied')
        keys = LIFECYCLE_KEYS[table]
        for row in actual:
            name = row[1]
            expected_type = 'INTEGER' if name in LIFECYCLE_INTEGERS[table] else 'TEXT'
            expected_pk = keys.index(name) + 1 if name in keys else 0
            nullable = name in LIFECYCLE_NULLABLE.get(table, set())
            expected_not_null = int(not nullable and (not expected_pk or len(keys) > 1))
            if (row[2].upper(), row[3], row[5]) != (expected_type, expected_not_null, expected_pk):
                raise ValueError(f'Unsupported source schema for {table}; no data copied')


def _validate_lifecycle_row(table, row):
    for name, value in zip(LIFECYCLE_COLUMNS[table], row, strict=True):
        if value is None:
            if name not in LIFECYCLE_NULLABLE.get(table, set()):
                raise ValueError(f'Invalid null in {table}.{name}; no data copied')
        elif name in LIFECYCLE_INTEGERS[table]:
            if type(value) is not int:
                raise ValueError(f'Invalid integer in {table}.{name}; no data copied')
        elif not isinstance(value, str) or '\x00' in value:
            raise ValueError(f'Unsupported text in {table}.{name}; no data copied')


def rehearse(source, connection, schema, *, include_lifecycle=False):
    """Atomically create and populate a new isolated schema from one SQLite snapshot.

    The caller supplies a fresh psycopg connection and remains responsible for its
    lifetime. A failure rolls back the new schema and every inserted row. No
    existing PostgreSQL schema is modified or replaced.
    """
    if not isinstance(schema, str) or not _SCHEMA.fullmatch(schema):
        raise ValueError('Rehearsal schema must use the tawzeef_rehearsal_ prefix')
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError('Rehearsal requires an idle PostgreSQL connection')
    source_db = _snapshot(source)
    counts = {}
    try:
        if include_lifecycle:
            _validate_lifecycle_schema(source_db)
        with connection.transaction():
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            connection.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
            for statement in _DDL:
                connection.execute(statement)
            if include_lifecycle:
                for statement in _LIFECYCLE_DDL:
                    connection.execute(statement)
            tables = TABLE_COLUMNS | (LIFECYCLE_COLUMNS if include_lifecycle else {})
            for table, columns in tables.items():
                insert = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                    sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, columns)),
                    sql.SQL(', ').join(sql.Placeholder() for _ in columns))
                sqlite_columns = ', '.join(f'"{name}"' for name in columns)
                keys = LIFECYCLE_KEYS.get(table, ('id',))
                sqlite_order = ', '.join(f'"{key}"' for key in keys)
                rows = source_db.execute(f'SELECT {sqlite_columns} FROM "{table}" ORDER BY {sqlite_order}')
                copied = 0
                with connection.cursor() as cursor:
                    while batch := rows.fetchmany(500):
                        for row in batch:
                            if table in TABLE_COLUMNS:
                                _validate(table, row)
                            else:
                                _validate_lifecycle_row(table, row)
                        cursor.executemany(insert, batch)
                        copied += len(batch)
                count = connection.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
                if count != copied:
                    raise RuntimeError(f'Row-count verification failed for {table}')
                _verify_rows(source_db, connection, table, columns, keys)
                counts[table] = copied
        return counts
    finally:
        source_db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path, help='Existing SQLite database, opened read-only')
    parser.add_argument('--schema', required=True, help='New schema named tawzeef_rehearsal_<suffix>')
    parser.add_argument('--include-lifecycle', action='store_true', help='Also copy selected interview, offer and notification tables; still incomplete')
    args = parser.parse_args()
    dsn = os.environ.get('TAWZEEF_POSTGRES_REHEARSAL_DSN')
    if not dsn:
        parser.exit(2, 'Set TAWZEEF_POSTGRES_REHEARSAL_DSN privately in the process environment.\n')
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            counts = rehearse(args.source, connection, args.schema, include_lifecycle=args.include_lifecycle)
    except (OSError, ValueError, sqlite3.Error, psycopg.Error, RuntimeError):
        # Database errors may include source row values; never print them.
        parser.exit(1, 'Rehearsal failed; no new schema was committed. Check source compatibility and database access.\n')
    print(json.dumps({'status': 'partial_rehearsal_only', 'copied_rows': counts,
                      'notice': 'Copied tables may contain sensitive data. This is not an application cutover.'},
                     sort_keys=True))


if __name__ == '__main__':
    main()
