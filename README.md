# TawzeefAI

A local demonstration of an HR and recruitment assistant. It uses synthetic jobs and applications, helps HR review evidence of skills and schedule interviews, and answers policy questions using sources permitted for each role. **Final decisions are human; this demo is not ready for real candidate data or public deployment.**

## Run on Windows

Requirements: Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/). From PowerShell in the project directory:

```powershell
& .\start-demo.ps1
```

Open <http://127.0.0.1:8000/>. The script creates a local database and enables demo accounts only; stop the server with `Ctrl+C`. Data persists in `backend/tawzeefai.sqlite3` between runs. Do not expose this server to a network: the following credentials are public.

You can also run a local demonstration with Docker Desktop using `& .\start-docker-demo.ps1` and open <http://127.0.0.1:18000/>. To enable experimental hybrid retrieval in the container after providing the local model, use `& .\start-docker-demo.ps1 -Hybrid`. Application data uses SQLite; the experimental retrieval index uses PostgreSQL/pgvector. The container's SQLite database is separate from the direct-run database. See [SETUP](SETUP.md) for setup and data details.

To try local résumé passage suggestions for job criteria on Windows, prepare the local embedding model as described in [experiment details](docs/local-semantic-experiment.md), then run `& .\start-demo.ps1 -SemanticScreening`. This can be combined with `-Google`. Suggestions are experimental, do not verify experience, and do not replace human review; use synthetic data only.

| Role | Username | Demo password |
| --- | --- | --- |
| Candidate | `candidate` | `candidate-demo-only` |
| Employee | `employee` | `employee-demo-only` |
| HR | `hr` | `hr-demo-only` |

`uv` uses the dependencies in `backend/uv.lock`. From the `backend` directory, run the tests and checks with:

```powershell
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked python evaluate.py
uv run --locked python evaluate_capstone.py
```

`evaluate.py` runs a preliminary evaluation of synthetic comparisons for a published job description and policy retrieval questions. The reference labels have not been approved by a human reviewer and the questions are relatively easy; this is not an independent measurement of hiring accuracy, fairness, or time savings.

HR can run a synthetic-sample report from the recruitment metrics interface. It shows source retrieval, comparisons, and negative cases; it does not measure answer correctness, human screening time, or fairness.

## Try local interview booking

1. Sign in as the candidate and submit a synthetic résumé to a job.
2. Sign in as HR, review the application, and choose **Advance to the next stage**. In Interviews, add an available time and interviewer; entered times use the device's time zone.
3. Return to the candidate account, open the advanced application, and choose an available slot. A successful booking appears under **My interviews**.

The application stores appointments locally and checks conflicts when booking. HR can cancel a future interview. Booking itself does not create an external event or send email; optional Google actions are separate and reviewed manually when the Windows integration is enabled.

HR can review and release a batch of interview windows targeted to specific applications. Each window is bound to the application and candidate at creation; the candidate chooses their own time. Permissions and conflicts are rechecked before the batch is released, and invitations are not sent automatically.

After an interview ends, HR can record that it took place. The action and actor are recorded, and the candidate sees the stage without HR assessment details. The interview is not assumed to have occurred merely because time passed. Another interview can be scheduled after the previous one is confirmed complete.

Once an interview is recorded as completed and no interview is in progress or upcoming, HR can record a job offer in the application. The candidate reviews and accepts or declines it; after acceptance, HR can record the hiring decision. Actions are audited without copying offer details into the audit log, and no message is sent automatically. Older applications not yet hired must complete these stages before a new hiring decision is recorded.

A candidate or HR can download an `.ics` calendar file for a scheduled interview and add it manually. A previously saved file does not update automatically when the appointment is cancelled.

To try optional Google actions on Windows, prepare Calendar and Gmail authorization as described in [integration details](docs/google-integration.md), then run `& .\start-demo.ps1 -Google`. HR can prepare a private Calendar event without attendees or a confirmation email to one recipient, then review and confirm each action separately. A synthetic private event was created and removed through the application and Google Calendar; actual Gmail delivery has not been tested. Interrupted or uncertain states may require external review. Removing a Google event does not cancel the local appointment or send a cancellation notice. Authorization is stored in Windows Credential Manager and is unavailable to the Linux container.

HR can also review a combined Calendar and email proposal and prepare two local drafts together. HR supplies the email address; the application does not verify ownership. Preparing drafts does not contact Google or send anything; each external action requires separate review and confirmation.

## AI connection

When a Groq key encrypted for the current Windows user is present at `.tooling/groq-key.dpapi`, the startup script loads it into the temporary process environment and enables generated answers. Otherwise the application shows document excerpts only. To run the server without the script, set `GROQ_API_KEY` and `TAWZEEF_ENABLE_GROQ=1` in the environment; do not put the key in code or a tracked file. When enabled, the question and authorized policy excerpts are sent to the provider; up to 500 characters of an eligible preceding question may also be sent as follow-up context. Use synthetic data only in this demo. Authorization is checked before the call and before saving, but the network call cannot be made atomic with a database transaction. A model answer can be wrong even when it cites a valid source ID, so review the source and decision with HR.

## Capabilities and limits

- Candidates can upload text-based PDF or DOCX files and view their applications and appointments. Image-only PDFs require OCR and produce an error rather than a claim of extraction. File size limits and explicit errors apply.
- HR can view résumé text, run a skill-mention screening with text evidence, and record a manual decision. Negation and instruction detection use limited heuristics and can fail; the result is not verified experience or a hiring decision.
- HR can define job criteria, link judgments to excerpts from a partially redacted résumé, and calculate a weighted score range based on reviewed coverage. A missing-evidence judgment requires the reviewer to attest that the source was examined. Rule-based redaction is limited and does not guarantee anonymity. Résumé section extraction relies on clear headings and shows unverified excerpts; unknown sections are not treated as absent.
- HR reviews show fields extracted from explicit labels or limited patterns in recognized sections, with positions in the partially redacted text available through the API. Record grouping is tentative, and fields not extracted remain unknown. Extraction does not verify experience or guarantee redaction, and image-only files are not OCRed.
- HR can select spans from partially redacted text and classify them manually as experience, education, or project fields. The application stores offsets, a review version, and a source fingerprint; selections must be redone if the résumé changes. Selections do not prove qualifications.
- A LangGraph review shows a summary of the current human assessment, its range and evidence, and follow-up questions for unknown criteria; it does not make a hiring decision. New assessments are bound to fingerprints of the criteria and redacted résumé. Older assessments without fingerprints remain in the record but require review before comparison, as do assessments affected by changed résumés, criteria, or evidence.
- When optional semantic résumé screening is enabled, a local model suggests excerpts for review from sections recognized by the parser. It can miss evidence or suggest negated or inappropriate text; similarity does not change judgments or decisions, and redaction is partial. See the [synthetic evaluation plan](docs/semantic-resume-evaluation.md).
- HR can compare current assessment ranges for a job. Overlapping or touching ranges do not establish a difference, and outdated or missing assessments are excluded. Clarifying questions are suggested for unknown or unproven criteria; review and decisions remain human.
- HR can configure a threshold per job to show review priority based on current assessment ranges. Outdated or missing assessments are inconclusive, and the classification does not automatically reject or hire anyone; every application remains available for review.
- Account administration is a separate capability from the HR role. It supports disabling accounts, resetting passwords, and revoking sessions for subsequent requests. Operations started before access was revoked may complete. See [SETUP](SETUP.md) for creating the first local administrator.
- General policy and job search is lexical, not general semantic search, and Arabic support is limited. Current documents, policies, and jobs are fictional.
- Optional hybrid retrieval for added policy documents uses a local model and PostgreSQL/pgvector. It can be enabled separately for answers and conversations and returns excerpts requiring review; a small development sample showed unsupported retrievals and incorrect abstentions. See [experiment and setup details](docs/local-semantic-experiment.md).
- HR can classify added knowledge documents as policies, job descriptions, or past-hire patterns and optionally link them to a job. Older documents remain unclassified and unlinked. Experimental hybrid retrieval filters uploaded documents by type and job; past-hire patterns are restricted to HR. Classification does not remove personal data; use synthetic data in the local demo.
- Text-based PDF policy documents are indexed page by page, with actual file page numbers that may differ from printed page numbers. Image-only pages require OCR. Older imports without page metadata must be reimported to display page numbers.
- New DOCX files using Word heading styles are split by heading and show the section name with the source. Older files and files without recognized headings retain the previous text splitting behavior; reliable DOCX page numbers are not available from this extraction.
- Conversations are saved and isolated by account. Access to the sources of old answers is rechecked when displayed. Certain short, explicit follow-up questions may use the most recent eligible question from the same conversation as limited context after role and source checks; the previous answer is not sent. Follow-up detection is limited and is not full conversational memory; state the topic explicitly if a follow-up is not understood.
- Candidates can choose one of their own applications in conversation to see its current stage. The stage is recalculated when the history is opened from authorized application data, without revealing HR notes or sending the question to an AI provider.
- Candidates can view a fresh keyword-mention screening preview based on their current résumé and the job requirements. It is not the recorded HR screening, verification of experience, or a hiring decision; negation detection and redaction are imperfect.
- HR can preview and explicitly share a reviewed rubric score range, criteria, judgments, and supporting excerpts with the application owner. The candidate sees only the shared summary. Relevant changes invalidate the release and require a new HR review; score ranges express uncertainty and do not establish a person's ability or a hiring decision.
- The conversation policy agent searches sample policies and authorized handbook sources only; it excludes job descriptions, past-hire patterns, and older unclassified documents. Its conservative lexical search may miss synonyms or evidence spread across passages. When enabled, the model may choose its search tool. It abstains when no eligible match is found; a valid citation ID does not prove semantic support. This mode does not use the previous question as context.
- Experimental hybrid reranking for the policy agent can be enabled by setting `TAWZEEF_USE_POLICY_HYBRID=1` in the server environment and configuring PostgreSQL, the local model, and hybrid retrieval as described in [experiment details](docs/local-semantic-experiment.md). It only reorders eligible lexical matches, does not establish synonym support, and falls back to lexical ranking with a notice if unavailable. One integration test passed with real PostgreSQL and pgvector on synthetic data; it does not establish ranking quality across multiple sources or answer correctness.
- Setting `TAWZEEF_POLICY_HYBRID_SUGGESTIONS=1` with the experimental hybrid setup additionally allows the policy conversation to show up to three authorized, unverified handbook passages when it otherwise abstains. They are possible leads, not answers or evidence of support; they are not sent to the model. Access is rechecked before saving and when reopening the conversation. The graph review continues to abstain without retaining these suggestions.
- The LangGraph review persists its state, pauses for human review, and resumes. HR can choose a plan that varies with application state: when Groq is enabled, the model proposes read steps from a limited list and the server validates them; otherwise a deterministic or fallback plan is used and its source is shown. The planner sends application status and whether screening exists, not the candidate's identity or résumé. This review path does not change the application, book a slot, or send email; resumption is rejected if the application or job has changed. A separate path can propose a shared slot after HR approval, which any eligible candidate may book. In the full path, Groq can also invoke a limited read-tool assistant. Policy results are reconstructed from sources still accessible at display time; references and fingerprints are stored without the question or excerpts in review state. This is not yet the complete five-agent system described in the presentation.
- The adaptive scheduling goal combines reading application status and availability with an HR-specified slot proposal and pauses for review. After approval, a shared slot is added for any eligible candidate to book, without sending email or creating a Google event. The server rechecks authorization, conflicts, and data before adding it.
- A separate graph review can propose a slot reserved for the current application and candidate. HR must review and approve the proposal; the candidate books it separately. Authorization and conflicts are checked again on approval, and this path does not send a message or create a Google event.
- HR can run a review-preparation assistant that uses Groq to choose limited read tools. It displays tool indicators locally and has no hiring, rejection, or scheduling tool. When those tools are selected, only job skill requirements and application and screening status indicators are sent to the provider; the résumé text and candidate name are not sent on this path. These restrictions do not guarantee general privacy or model accuracy.
- Interviews are booked locally; optional Google actions are separate, and Calendar events created by the application do not add attendees. Metrics are computed from database records; measuring time savings requires an actual human study.
- Reference data under `data/fixtures/` and `data/capstone_samples/` is synthetic, and the proposed rankings have not been approved by a human panel. Example tests do not prove absence of bias or leaks in general and do not measure answer accuracy or time savings on real data.

See the [requirements](docs/requirements.md) and [evaluation plan](docs/evaluation-plan.md) for details and outstanding work.
