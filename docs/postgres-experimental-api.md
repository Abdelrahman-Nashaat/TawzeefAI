# Experimental PostgreSQL API

This opt-in development slice is for synthetic data only. It is not the full
application or a migration. It stores demo users, jobs, sessions, applications,
notices, audit events, screening records, slots, and interviews in one PostgreSQL schema.
SQLite data is not read or migrated. Only health, login, logout, session identity,
jobs, application submission and listing, HR keyword screening and advancement,
local candidate interview booking, cancellation, explicit HR completion, local
cancellation notices, and offers with candidate response and explicit HR hire
are implemented. A dedicated browser interface supports these actions. Graph
workflows, calendar and email delivery, policy chat, and the dashboard remain
unavailable in this mode.

HR may reject a submitted, advanced, or offered application with a nonempty
reason through the browser or API. The decision atomically withdraws a presented offer and cancels future
interviews, creating candidate notices and audit entries. An accepted offer
blocks rejection because this experimental mode has no accepted-offer
resolution workflow. No email or external calendar action is sent.

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
Booking is recorded locally; no email or calendar invitation is delivered.
For future interviews, HR may call `POST /api/interviews/{id}/cancel`, or the
owning candidate may call `POST /api/interviews/{id}/candidate-cancel`. The
cancellation, audit event, and local notice to the other role are committed in
one transaction. Candidates may then choose another available slot. The local
inboxes are `GET /api/candidate/interview-cancellations` and
`GET /api/hr/interview-cancellations`; each notice can be marked read through
its corresponding `POST .../{id}/read` endpoint. HR read state is per account.
No message or external calendar action is sent. Once an interview's end time has
passed, HR can explicitly confirm it took place with
`POST /api/interviews/{id}/complete`. The confirmation and audit record are
atomic; the candidate sees `completed` in interview listings and can book a
later slot. Time passing alone does not record attendance. On startup this mode
adds a completion flag and replaces the application booking index in its own
experimental schema; stop all older server versions before upgrading.
After at least one completed interview, and when no interview is active or
upcoming, HR can present a local offer with `POST /api/applications/{id}/offers`
and `{"details":"..."}`. The owner and HR can list offers with
`GET /api/applications/{id}/offers`. Only the intended candidate may accept or
decline via `POST /api/offers/{id}/respond`; HR may withdraw an unanswered
offer through `POST /api/offers/{id}/withdraw`. After acceptance, HR can record
hiring with `POST /api/applications/{id}/decision` and `{"decision":"hire"}`.
These transitions are stored and audited atomically, and they never send email.
Offer contents are not copied to the audit record. This is a synthetic local
workflow, not a complete production offer process.
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
matter. Open `http://127.0.0.1:18080/` for the dedicated demo interface. It
uses only the PostgreSQL API, not the main SQLite interface, and supports the
candidate and HR demo accounts. Screening remains provisional: HR must inspect
the CV and explicitly advance an application. Slot times use the browser's
local time zone. Unsupported endpoints return 404. Run the real PostgreSQL tests with
`TAWZEEF_TEST_POSTGRES_DSN` set privately to a disposable test database and
`pytest -q tests/test_postgres_experimental_api.py tests/test_postgres_scheduling.py tests/test_postgres_cancellation_lifecycle.py tests/test_postgres_interview_completion.py tests/test_postgres_offers.py`. The tests create and remove
randomly named test schemas. No success claim extends beyond the implemented
routes and tested scenarios.
