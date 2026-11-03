# Optional local Streamlit interface

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
