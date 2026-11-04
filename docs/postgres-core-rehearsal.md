# Experimental PostgreSQL core-data rehearsal

This offline rehearsal copies and verifies four core tables by default: `users`,
`jobs`, `applications`, and `audit_events`. The optional `--include-lifecycle`
flag also copies `interviews`, `interview_completions`, `interview_slots`,
`job_offers`, `hr_notifications`, `hr_notification_reads`, and
`slot_audit_events`. The application continues to use SQLite; neither option
produces a complete migration or operational database. Sessions, admin
capabilities, recruitment reviews, documents, graph checkpoints, and other data
and behavior remain excluded. Do not use the resulting schema as an operational
database or run this experiment on real candidate or employee data.

The offer recipient column is copied as stored, including nulls for legacy
offers whose original recipient has not been established. See
[offer recipient migration](offer-recipient-migration.md).

The source must be a current-format synthetic SQLite database. The code opens it
read-only, starts one consistent read snapshot, checks the exact expected table
columns and SQLite foreign keys, and copies into a **new** PostgreSQL schema.
An existing schema is never replaced. The destination transaction creates the
selected tables and their indexes, enforces their defined foreign keys, then
compares every copied value with the source snapshot before committing. Unsupported schema,
types, nulls, embedded NULs, foreign-key errors, or verification failures abort
the transaction. The process may hold a SQLite read transaction for the duration
of the copy; run offline with a disposable database. Timestamps and JSON fields
remain text to preserve the source values rather than guess conversions.

The tables include password hashes and résumé text if present in the source.
The command prints only counts and a partial-rehearsal marker, but PostgreSQL
stores copied values in full. Use synthetic data and a private disposable test
database, restrict access, and never place a database connection string in the
repository or terminal history. Database connection credentials belong in a
temporary process environment variable.

## Verify with a disposable PostgreSQL database

Prepare a separate disposable PostgreSQL database and install the repository's
locked `postgres` extra in an isolated environment. Set
`TAWZEEF_TEST_POSTGRES_DSN` privately to its connection string, then run from
`backend`:

```powershell
uv run --locked --extra postgres pytest -q tests/test_postgres_core_rehearsal.py
```

The tests create randomized schemas with a `tawzeef_rehearsal_test_` prefix,
populate them only from freshly created synthetic SQLite databases, and drop only
those generated schemas at completion. They verify value equality, foreign keys,
rollback, source changes during copying, rejection of unknown source columns,
and non-overwrite of an existing schema. Without the DSN or optional dependency,
the real PostgreSQL tests are skipped and must not be reported as passing.

## Run an offline synthetic rehearsal

Set `TAWZEEF_POSTGRES_REHEARSAL_DSN` privately to a disposable PostgreSQL
database. Then use a new schema with the required prefix and a separate
synthetic source:

```powershell
uv run --locked --extra postgres python -m app.postgres_core_rehearsal --source path-to-synthetic.sqlite3 --schema tawzeef_rehearsal_example
```

To include the seven selected lifecycle tables, add `--include-lifecycle` to the
command. This option requires the expected current schema for all selected
tables, and it still leaves the remaining application data and behavior out.

The caller owns and must later remove the schema using their normal PostgreSQL
administration process. This command never deletes or overwrites one. Do not use
the printed counts as evidence of complete migration: full schema and data
coverage, application query and transaction changes, authorization boundaries,
LangGraph checkpoint migration, external integration locking, consistency at
cutover, recovery, and end-to-end PostgreSQL tests remain required.
