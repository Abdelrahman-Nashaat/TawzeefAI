# Optional local Streamlit interface

The presentation proposes Streamlit for chat and the HR dashboard. This optional
interface uses the existing FastAPI server and covers saved knowledge
conversations, HR metrics, and candidate application submission, status,
screening previews, shared assessments, and interview booking. Use the main local
interface for HR résumé review and administrative controls. Booking is local and
does not send an email or create a Google Calendar event.

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
