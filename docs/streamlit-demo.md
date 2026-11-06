# Optional local Streamlit interface

HR can also start a graph offer review for an eligible application and inspect
the current candidate, job and exact terms before explicit approval. Changed or
expired reviews cannot be approved and can be discarded. The offer is presented
locally; no message is sent. If the result is uncertain, inspect the application
before retrying. Changing the selected application removes the review from this
Streamlit session; an undiscarded or uncertain draft can remain on the server
until it expires and a later offer review performs cleanup.

For graph offer and rejection reviews, HR may include an optional policy
question. The review shows currently authorized source excerpts or states that
no answer was found. If a cited source becomes unavailable, approval is hidden;
discard the review and start again. Excerpts require human review and are not
proof of relevance. When the AI provider is enabled, the question and authorized
policy passages may be sent to it. The graph checkpoint does not retain the
question or excerpts; the form clears its question after submission and sign-out.

HR can start a graph keyword-screening review from the application queue. The
interface reloads the current server preview before approval, displays the
candidate ID and scoring details, and requires explicit confirmation. Approval
records only a screening; it does not advance, reject, hire, or send a message.
Changed inputs invalidate the approval. This keyword score is provisional and
does not establish qualifications.

The presentation proposes Streamlit for chat and the HR dashboard. This optional
interface uses the existing FastAPI server and covers saved knowledge
conversations, HR metrics, and candidate application submission, status,
screening previews, shared assessments, interview booking, and reviewing and responding to job offers. HR can review a bounded masked résumé preview and record an advance or rejection with a reason; the server binds decisions to the reviewed snapshot. Use the main local interface for advanced HR review and administrative controls. Booking is local and
does not send an email or create a Google Calendar event.

For an advanced application, HR can preview and release a private slot using
explicit UTC-offset times. HR can inspect targeted slots and explicitly withdraw
an unbooked future slot; the server rechecks the candidate and application binding.
The candidate chooses the slot in their interface. After an interview ends, HR may explicitly confirm that it took place. An offer
can be presented only after a confirmed interview and when no interview is
active; the API rechecks eligibility. After the candidate accepts, HR can
explicitly record a reviewed hiring decision. If a write's result is uncertain, inspect
the current application or interview before attempting another action.

HR can view and acknowledge local candidate-booking notices; each HR account
has its own read status. Notices show the interview's current status and are
created for new bookings only. No booking email or calendar event is sent.

Candidates can request cancellation of an eligible locally booked future
interview after confirming the action. The server refuses cancellations that
need external coordination and directs candidates to contact HR. A successful
cancellation creates a local notice that HR can view and mark read in their own
inbox. It sends no email and does not change Google Calendar.

HR can also start a graph rejection review with a reason, inspect the candidate,
role, affected interviews, private slots and offer, then explicitly approve or
discard it. A changed review cannot be approved and may be discarded. The API
rechecks current permissions and context; approval records local changes only
and sends no message. If a response is uncertain, check the application before
retrying.

On Windows, run the API and the Streamlit UI in separate PowerShell windows:

```powershell
& .\start-demo.ps1
```

```powershell
& .\start-streamlit.ps1
```

Open http://127.0.0.1:8501/. Both servers use localhost and synthetic demo
data. The Streamlit script installs the locked optional `streamlit` dependency
set with `uv` into a separate ignored `.tooling/streamlit-venv` environment,
so it does not change the running API's Python environment. If the API is running on another local port, pass
`-ApiPort <port>`. Stop each server with Ctrl+C.

The client connects only to `127.0.0.1` and keeps the API session cookie in
the individual Streamlit session. The API rechecks identity and permissions
on each request. A browser refresh may end the Streamlit session and require a
new sign-in. Sign-out asks the API to revoke the session and clears the local
client; if the API cannot be reached, server revocation cannot be confirmed.
The UI uses Streamlit's cross-origin and XSRF protections and binds to
loopback. This local demo is not configured for public exposure or real
applicant data. Authorized policy excerpts and questions can be sent to the
configured model, and its answers require source review.
