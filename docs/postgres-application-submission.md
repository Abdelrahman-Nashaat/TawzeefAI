# PostgreSQL application submission migration work

The existing SQLite application endpoint uses the shared submission operation
in `app/application_store.py`. That operation has also been tested with
PostgreSQL transactions. It stores the application, local HR notice, audit
event, and pending screening snapshot in one transaction. The PostgreSQL path
locks the job row while taking the screening revision, so a concurrent job
update cannot invalidate the initial snapshot before it is recorded.
This guarantee requires every future PostgreSQL writer that changes a job or
its screening revision to lock or update that same job row first and update
the revision in the same transaction. Updating the revision independently
would bypass the lock. The synthetic test follows this ordering; it does not
establish that a production PostgreSQL schema enforces it.

This is an incremental migration component, not a PostgreSQL-backed endpoint
or deployment. Authentication, authorization, the application schema and its
revision triggers, and other application operations are not switched to
PostgreSQL. A future PostgreSQL caller must use named rows, disable autocommit,
authenticate and authorize the current candidate in the same transaction,
coordinate role changes with that check, and commit or roll back the complete
transaction. No data is copied by this component. The present local demo
continues to use SQLite.

To exercise the PostgreSQL tests against a dedicated disposable database, set
`TAWZEEF_TEST_POSTGRES_DSN` privately and run from `backend`:

```powershell
uv run --locked --extra postgres pytest -q tests/test_postgres_application_store.py
```

The tests create and remove a randomly named synthetic schema. If the DSN is not
set, they are skipped; they do not constitute an end-to-end PostgreSQL migration
or a production-schema test.
