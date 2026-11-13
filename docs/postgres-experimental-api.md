# Experimental PostgreSQL API

HR can also start a **resumable review** or advancement proposal from the
browser. This variant commits each of five specialist stages separately. The
review can be reopened after interruption and continued from the next stage;
approval is available only after all stages complete and HR inspects the
review. The API supports a private-slot proposal at
`POST /api/applications/{id}/graph-review-preparations`; subsequent calls to
`POST /api/graph-review-drafts/{id}/prepare` run one stage each. The browser
also supports advancement with a candidate-specific private slot in this flow.
A changed source or
availability invalidates the draft, which can still be discarded. Concurrent
requests serialize on the draft and scheduling locks.

Each step recomputes the full source snapshot for safety and compares it with
the saved fingerprint; the specialist graph resumes from its persisted stage.
Only digests and bounded status flags enter checkpoint state, not résumé text
or policy excerpts. Digests of low-entropy inputs may still be guessable. The
final specialist combines prior matching, application-status and slot flags
into a deterministic suggested next step; it is not a decision or a model-led
agent. HR approval revalidates current state and atomically records an advance
and optional slot. A discarded in-progress review leaves its paused checkpoint
in the synthetic demo database, but the closed draft cannot be continued.

The original supervised-review path can also save a review and reopen it after restart. The
database retains the policy question, reviewer, status and a fingerprint;
it does not retain the graph output or source excerpts. Reopening recomputes
the review, and acknowledgment requires the source, results and availability
to remain unchanged. A stale review can be discarded. An advance proposal is
available for a submitted application with a current HR rubric assessment; it
advances only after explicit HR approval and records the decision atomically.
An ordinary review acknowledgment makes no recruitment decision. This is an
LangGraph approval interrupt persisted with the PostgreSQL checkpointer. The
checkpoint stores the draft ID, intent, fingerprint and individual specialist
output digests, but not graph output, source passages or the question. The five
specialist nodes run before the approval interrupt and their outputs are checked
against the review snapshot; the latest outputs are independently recomputed
and checked again before approval. Approval resumes the checkpoint in the same
database transaction as the HR action and audit, after rechecking current
sources and authorization. Reviews made before checkpoint support retain their
earlier application-level behavior. Individual checkpoints contain output
digests rather than plaintext, but low-entropy outputs may be guessed from
their hashes. In this original path the specialist nodes do not exchange intermediate results
or provide autonomous planning. Their checkpoints are committed together
when draft creation succeeds; a failure during creation restarts all stages. The
resumable variant described above persists stages separately.
Use synthetic information
only; questions and checkpoint records remain until the demo database is removed.

The advance draft API may additionally receive a `slot` object with `start_at`,
`end_at`, and `interviewer`. The proposal is saved with the draft and returned
when HR reopens it. Approving the unchanged draft rechecks candidate eligibility
and time conflicts, then advances the application and creates a private slot
in the same transaction. The candidate must still book that slot. Discarding a
draft makes neither change. HR can create and review this proposal in the
experimental browser interface. No email or calendar action is performed. Slot proposal details persist in the
demo database, so use synthetic information.

The demo also supports saved, role-scoped extractive knowledge conversations.
Each account may create up to 100 conversations with up to 100 questions each.
Questions and source identifiers with fingerprints are stored in PostgreSQL;
source passages are reconstructed only while the documents remain active,
unchanged, and accessible to the account's current role. Changed or revoked
sources make that saved turn unavailable. Conversations are not portable across
role changes. Owners can delete conversations for their current role and their
saved questions through the browser or API. Use only
synthetic questions and documents. Saved conversations do not use hybrid search,
model-generated answers, or an external service, and excerpt relevance is not
verified.

This opt-in development slice is for synthetic data only. It is not the full
application or a migration. It stores demo users, jobs, sessions, applications,
notices, audit events, screening records, slots, and interviews in one PostgreSQL schema.
SQLite data is not read or migrated. Health, login, logout, session identity,
jobs, application submission and listing, HR keyword screening and advancement,
local candidate interview booking, cancellation, explicit HR completion, local
cancellation notices, and offers with candidate response and explicit HR hire
are implemented. A dedicated browser interface supports these actions and saved
extractive knowledge conversations. Calendar and email delivery remain unavailable
in this mode.

HR can run a read-only LangGraph review from an application. Five deterministic
specialists summarize parsed sections, the current audited rubric range, status,
available slots and optional lexical policy sources. This immediate review does
not save graph state or call a provider, and cannot advance, reject, book or send mail.
Availability is a preview; refresh and recheck before a separate booking. This
partial path does not implement the full multi-agent workflow in the slides.

HR can create synthetic jobs and edit a job while it has no applications using
the browser or `/api/hr/jobs`. Once any application is submitted, that job is
immutable: create a new job ID for revised requirements. This preserves the
meaning of earlier screenings and decisions. Concurrent HR edits require a
fresh revision. Job create and edit operations are recorded in `job_audit`.

Candidate, employee and HR accounts can search authorized synthetic knowledge
documents. HR can add text documents with a title, audience, collection and
optional page; past-hire patterns are restricted to HR. Search filters documents
by role before lexical ranking, returning excerpts and citations or an abstention.
The default is lexical and extractive; it is not a generative answer or the
full vector RAG flow.
An optional **Generate answer using Groq** button asks for confirmation before
sending the question and role-authorized, lexical-matched source excerpts to
Groq. It requires `TAWZEEF_POSTGRES_ENABLE_GROQ=1` and a `GROQ_API_KEY` in the
server environment; it is disabled by default. Do not use real candidate or
organizational data in this synthetic demo. The answer is displayed only when
each substantive sentence or line includes a retrieved citation ID; invalid or
missing citations or provider failure fall back to excerpts. Full-width citation
brackets are normalized before checking IDs. This conservative format check can
reject valid answers. No provider call is made when retrieval abstains.
A valid citation does not prove factual correctness or protect against prompt
injection; compare the answer with the source. Generated answers are not stored
in saved conversations. Provider access is limited to one request per process;
authorized source rows remain locked through the call, which can delay document
changes for the provider timeout. Other server processes have separate limits.
On 13 November 2026, one synthetic candidate query through the PostgreSQL API
returned a generated answer with a permitted page-2 citation using the live
provider. This single development check is not an answer-quality evaluation or
evidence of reliability across policies, languages, or access scenarios.
Document changes are audited without storing document text in the audit row.
Documents are limited to 4,000 characters each and 500 active documents; the
local interface accepts synthetic material only.
HR can list active document metadata in the browser or through
`GET /api/knowledge/documents`, and deactivate documents from the browser or via
`POST /api/knowledge/documents/{id}/deactivate`. The list excludes document bodies;
deactivated documents no longer appear in search or the active document list.
An optional **Experimental hybrid search** button also computes local E5
embeddings over role-authorized passages and searches them using pgvector. It
combines semantic and BM25 ranks with reciprocal rank fusion, reordering only
passages that meet the lexical match gate. This is not a learned reranker and
has not been shown to improve answer quality. The lexical excerpts remain
distinct from semantic-only suggestions: suggestions
are unverified leads, not answers or proof of support. This path requires the
`postgres` and `semantic` extras, the pinned local model, an operator-provisioned
pgvector extension, `TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID=1`, and
`TAWZEEF_EMBEDDING_MODEL_DIR`. See `local-semantic-experiment.md` for the model.
The application never installs the database extension. It limits each query to
200 authorized passages, re-embeds them on every request, uses an uncalibrated
0.80 similarity cutoff, and returns 503 if the
optional path is unavailable; the standard lexical search remains available.
Embeddings are retained in `knowledge_vectors` in the experimental schema,
including vectors of later-deactivated documents, until the schema is removed.
Search always restricts vector IDs to current authorized active documents. Use
only synthetic data. No external model or network service is called.
New experimental schemas include an `employee` demo account using the same
demo-password convention as other accounts. Existing schemas are not reseeded;
create a new disposable schema to use that account if it is absent.

The HR-only dashboard reports current application statuses, recorded screening
events, and time to hire by job. The average is the number of days from
submission to the latest recorded hire decision for currently hired applications;
missing, invalid or negative durations are excluded. It is not a comparison
with manual screening or evidence of time saved. No applicants yields no average.
The browser visualizes current status counts relative to each job's retained
applications. It also reports distinct applications with recorded screening,
advance, completed-interview, presented-offer, hiring and rejection events.
Milestones can overlap and are not conversion rates; repeated events are counted
once per application. A rubric review alone does not count as keyword screening.
Applications removed from the database are excluded from the snapshot.

HR may reject a submitted, advanced, or offered application with a nonempty
reason through the browser or API. The decision atomically withdraws a presented offer and cancels future
interviews, creating candidate notices and audit entries. An accepted offer
blocks rejection because this experimental mode has no accepted-offer
resolution workflow. No email or external calendar action is sent.

Each new submission also creates a local HR notice in the same transaction.
HR can open the paginated inbox in the demo or via `GET /api/hr/submission-notices`
and mark a notice read for their own account. It does not change the application
or send an external message. Notices contain IDs rather than résumé contents;
existing applications without a recorded notice are not backfilled.

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
For an advanced application, HR can reserve a future slot specifically for
its current candidate with `POST /api/applications/{id}/private-slots` and
list its upcoming reservations with `GET` on the same path. The candidate can
see and book it only for that application. HR can withdraw an unbooked private
slot with `POST /api/slots/{slot_id}/withdraw`; cancel an existing booking first.
Rejection or hiring withdraws the application's private slots. The browser
interface supports reservation and withdrawal in the HR application card.
An application whose owner changes cannot use the former owner's reservation.
On startup the experimental schema adds reservation columns; stop older server
versions before upgrading. No invitations or external messages are sent.
Booking is recorded locally; no email or calendar invitation is delivered.
Candidates may view their current persisted stage in the browser or via
`GET /api/workflows/{id}/progress`. The stage excludes HR notes and scores;
an interview becomes interviewed only after HR confirms completion.
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

## Experimental HR rubric API

HR can explicitly share the current reviewed score range and an HR-written
candidate-facing explanation using the browser or
`POST /api/applications/{id}/candidate-feedback`. The candidate sees only that
range and explanation via the browser or `GET` on the same path; private rubric
judgments and CV excerpts are not included automatically. HR must review the
exact text before sharing and can retract it with `DELETE` on that path. A new
assessment or changed CV or job makes a prior publication unavailable while
the saved source is no longer current. Feedback is only returned while the
stored recipient owns the application. Publishing and retraction write audit events
without copying the explanation to the audit log. Previously seen information
cannot be recalled from the candidate. Replacing or retracting feedback removes
the previous explanation; the audit log records the action but cannot reconstruct
that text. This remains a synthetic local demo.

HR can compare two to twenty applications for the same job using
`POST /api/hr/rubric-comparison` with `{"application_ids": [1, 2]}`. Each must
have a current, intact, source-bound human assessment with at least one scored
category. The response includes reviewed ranges and reports one range as higher
only when the ranges do not overlap. Overlap is indeterminate, not a tie or
qualification verdict. Reviews are subjective; this endpoint does not change
any decision or status. The HR demo browser provides checkboxes and a comparison
button for this workflow. Synthetic data only.

The assessment source fingerprint now includes the job identifier. Reviews
created before this change are marked stale and require a fresh HR assessment;
their history remains available. Stop older server versions before upgrading.

In the PostgreSQL demo browser, HR can open the weighted rubric for a submitted
application, select evidence from the partially masked CV and enter scores, or
leave categories unknown. Selections are source-bound and become stale if the
CV or job changes. This is a subjective HR review, not verified qualification
or an automatic hiring decision; masking is incomplete.

HR can use `GET /api/applications/{id}/rubric-source` to obtain the partially
masked résumé, job requirements and a source digest. `POST
/api/applications/{id}/rubric-assessment` saves exactly five human judgments:
`mandatory_skills`, `preferred_skills`, `experience`, `projects`, and
`education`, weighted 40/15/20/15/10. Each judgment contains an integer score
from 0 to 100 and nonempty `start`/`end` offsets into the returned masked text,
or `null` for an unknown category without offsets. Unknown categories contribute
a range of possible scores, not an assumption of missing qualifications. The
reviewer submits the source digest and must review again if the résumé or job
changes. HR may retrieve a current assessment with GET on the same endpoint.

These generic category scores are subjective HR judgments. Source offsets merely
identify text; they do not verify qualifications, accuracy, or fairness. Masking
is incomplete, and the original requires human review. The latest assessment is available separately; each new review is also retained
in append-only HR-only history at `GET /api/applications/{id}/rubric-history`,
with a cursor, stale-source flag, and audit-integrity check. Historical offsets
cannot reconstruct the original source after it changes; source text is not
retained in history. Audit records contain hashes rather than résumé excerpts.
Existing pre-history assessments are not backfilled. HR may explicitly advance
using either the recorded keyword screen or a current rubric review. For the latter,
send `{"decision":"advance","rubric_event_id": N}` to the decision endpoint,
where `N` is the `audit_event_id` returned by saving the review. The review must
be the latest, source-current and integrity-checked, with at least one scored
category. The decision records this event ID, not résumé text. A later review or
source change invalidates the proposed action. This is an API-only synthetic-data
experiment: saving an assessment itself does not advance, reject, or schedule.

## Knowledge metadata filters

HR can preview and import a synthetic text-based PDF in the PostgreSQL demo.
The preview displays extracted lines for selecting section headings; a section
continues across physical pages until the next selected heading. The import
re-extracts the same PDF and requires a matching preview fingerprint. Stored
segments retain physical page, selected section and the SHA-256 of the original
PDF bytes. The PDF itself is not retained. Segments are managed individually;
duplicate imports are possible. Empty pages create no segment. OCR is not
available in this path; mixed text and image-only pages are rejected. Extracted
text may still omit visual content on pages with a text layer, so review it
against the source. Uploads are limited to 5 MiB, 100 pages, 200,000 extracted
characters, 5,000 preview lines and 100 resulting segments. The existing
500-active-segment capacity applies atomically to each import.
At most two PDF previews and imports per application process can buffer or
extract at once; additional requests receive HTTP 503 and can be retried.
If the worker executor cannot start a task, PDF processing is disabled in that
process until the server is restarted; requests receive HTTP 503.

HR can optionally record a job ID and section for each synthetic text document.
Users can filter extractive, experimental hybrid, and saved-conversation searches
by exact job ID and section. Only currently authorized documents are considered;
returned sources include the supplied metadata. The job ID is a label, not a
verified foreign-key link, and section names are entered by HR rather than
automatically detected. Existing documents retain empty metadata. Saved excerpts
become unavailable when their source metadata changes; previously saved excerpts
from documents with no metadata remain compatible. This does not provide a
complete section-based ingestion pipeline or establish answer accuracy.
