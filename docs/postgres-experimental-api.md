# Experimental PostgreSQL API

This opt-in development slice is for synthetic data only. It is not the full
application or a migration. It stores demo users, jobs, sessions, applications,
notices, audit events, screening records, slots, and interviews in one PostgreSQL schema.
SQLite data is not read or migrated. Only health, login, logout, session identity,
jobs, application submission and listing, HR keyword screening and advancement,
and local candidate interview booking are implemented. Graph
workflows, calendar and email delivery, policy chat, the dashboard, and browser
interface remain unavailable in this mode.

HR can call `POST /api/applications/{id}/screen` for a submitted application.
The score, screening history, and audit record are saved together. This applies
automatic identity masking, which is incomplete and has not been reviewed by a
human in this mode. It is not a blind-screening guarantee or a hiring decision.
Candidates cannot run screening or see its score. Use synthetic data only.

HR can explicitly advance a submitted application with a recorded current score
using `POST /api/applications/{id}/decision` and `{"decision":"advance"}`; the
score does not itself decide advancement. HR can create future slots with
`POST /api/slots`. An advanced candidate can list available slots with
`GET /api/slots?application_id=...` and book one with
`POST /api/slots/{id}/book`. `GET /api/interviews` lets a candidate retrieve
their own bookings and HR retrieve all bookings, including after restart.
Booking is recorded locally; no invitation or notification is delivered.
Cancellation and interview completion are not implemented in this mode, so a
scheduled interview continues to block another booking for that application.
This mode exposes no job or resume editing, and does
not detect direct database changes to previously screened inputs.

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
`pytest -q tests/test_postgres_experimental_api.py tests/test_postgres_scheduling.py`. The tests create and remove
randomly named test schemas. No success claim extends beyond the implemented
routes and tested scenarios.
