# TawzeefAI frontend

This is a buildless Arabic RTL local-development interface. Serve this folder from the **same origin** as the backend API. It does not make cross-origin requests or store authentication tokens in browser storage. The API provides an HttpOnly session cookie.

## API contract

- `POST /api/auth/login`: JSON `{ "username": "...", "password": "..." }`; on success a JSON user `{id, username, display_name, role}` and a session cookie; 401 for invalid credentials.
- `GET /api/auth/me`: JSON user in the same shape; 401 if there is no valid session.
- `POST /api/auth/logout`: 204 and session cookie cleared.
- `role` is `candidate`, `employee`, or `hr`. User-provided values are inserted with `textContent`.

Authenticated candidates and HR can list jobs and applications; candidates can submit a PDF with a text layer or a DOCX as raw bytes with `job_id` and `filename` query parameters. HR can view application details, run the keyword-mention screen, and record a human decision with a note. All application data comes from the server. The score reflects mentions, not verified experience or an automated hiring decision. The frontend sends `POST /api/applications/{id}/decision` with `{decision, note}`, where `decision` is `advance`, `reject`, or `hire`.

The knowledge form posts `{question}` to `POST /api/knowledge/answer` and shows the returned answer mode and source excerpts. The default backend uses lexical retrieval, which can miss paraphrases and Arabic questions; an enabled external provider may generate an answer. The interface does not assert that those answers are correct.

Candidates and HR may view `GET /api/interviews`; HR may record an appointment with `POST /api/interviews`, using the browser's local time converted to UTC. Booking itself does not create external calendar events or send invitations. HR can prepare separate local Calendar and Gmail requests, review each request and explicitly confirm dispatch. Actual dispatch requires a configured Google provider; an unavailable provider reports an error. The UI shows unconfirmed or uncertain results without retrying the provider action automatically. HR metrics come from `GET /api/dashboard` and refresh after screening or a decision. Server-side authorization remains essential. Demo accounts only exist when backend demo seeding has been explicitly enabled; this UI does not activate it. Scanned image-only PDFs do not have OCR support.

For a confirmed Calendar delivery, the UI can request removal after a separate explicit review. The delivery list must expose `calendar_removal_state` as `null`, `removing`, `removed`, `rejected`, or `uncertain`; if it is absent or unknown, removal controls are withheld. Removal does not cancel the local interview or send a cancellation email. A pending or ambiguous removal must be reviewed rather than retried automatically.

HR can withdraw a future, unbooked interview slot. The server checks its current state atomically, so a concurrently booked slot cannot be withdrawn. Withdrawn slots remain visible to HR and are no longer offered to candidates. Withdrawal does not cancel a booked interview or send an email.

Checks: `node --check frontend/app.js`. End-to-end verification requires the backend running on the same origin and the browser to permit cookies.
