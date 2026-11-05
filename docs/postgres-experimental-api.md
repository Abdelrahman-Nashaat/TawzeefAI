# Experimental PostgreSQL submission API

This opt-in development slice is for synthetic data only. It is not the full
application or a migration. It stores demo users, jobs, sessions, applications,
notices, audit events, and pending screening records in one PostgreSQL schema.
SQLite data is not read or migrated. Only health, login, logout, session identity,
jobs, application submission, and application listing are implemented. Graph
workflows, screening, scheduling, policy chat, the dashboard, and browser
interface remain unavailable in this mode.

Use a disposable database and a PostgreSQL user allowed to create schemas.
Configure the DSN privately in the process environment; never commit credentials.
Choose a new schema named `tawzeef_exp_` followed by 8–32 lowercase letters or
digits. The application creates and seeds it on first start, retains its data
across restarts, and rejects existing schemas without its version marker. The
schema owner remains responsible for removing the test schema later.

From `backend`, after installing the optional PostgreSQL dependencies:

```powershell
$env:TAWZEEF_DATABASE_ENGINE = 'postgres-experimental'
$env:TAWZEEF_DEMO = '1'
$env:TAWZEEF_POSTGRES_SCHEMA = 'tawzeef_exp_example01'
# Set TAWZEEF_POSTGRES_DSN privately for the disposable database.
uv run --locked --extra postgres uvicorn app.main:app --host 127.0.0.1 --port 18080 --no-proxy-headers
```

The server uses public demo passwords and must run only on loopback. Do not put
it behind a proxy or use real candidate data. The application rejects remote
peers and untrusted Host headers, but network and proxy configuration still
matter. Unsupported endpoints return 404. Run the real PostgreSQL tests with
`TAWZEEF_TEST_POSTGRES_DSN` set privately to a disposable test database and
`pytest -q tests/test_postgres_experimental_api.py`. The tests create and remove
randomly named test schemas. No success claim extends beyond the implemented
routes and tested scenarios.
