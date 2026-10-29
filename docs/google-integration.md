# Google Calendar and Gmail integration: development status

This is an unfinished local integration. OAuth credentials can be authorized on
Windows, and provider calls and a durable delivery outbox are covered by tests
with fake Google responses. The application does **not** configure a live
dispatcher yet. No real Calendar event or email has been tested by this code.

Before enabling live dispatch, implement and test reconciliation of Calendar
events after confirmation or an ambiguous result, recovery from interrupted
delivery, editing or replacing safely rejected drafts, and a clear cancellation
notification workflow. A confirmed Calendar delivery currently blocks local
cancellation because no reconciliation path exists. An email already sent is not
automatically retracted or followed by a cancellation notice. Never retry an
ambiguous Gmail send automatically: the first attempt may have succeeded.

## Preparing a test account

The account owner needs a Google Cloud project with Google Calendar API and
Gmail API enabled, an OAuth consent configuration, and an OAuth client of type
Desktop app. Add the intended Google account as a test user if the consent
configuration requires it. Download its client JSON to a private local location
outside the repository; do not paste the file contents into chat or commit it.
The app's OAuth client is separate from any Codex Gmail or Calendar connector.

With the project's Windows Python environment and optional Google dependencies
installed, the following commands initiate separate consent flows, each for one
service, and store the resulting refresh credential in the current Windows
user's Credential Manager. Substitute the path without exposing the file:

```powershell
cd backend
uv sync --all-extras
uv run python -m app.google_auth calendar --client-file "C:\private\client.json"
uv run python -m app.google_auth gmail --client-file "C:\private\client.json"
```

The user must finish each consent in their browser. Granting consent alone does
not enable the app's live dispatcher. The credentials are shared by the local
application, not per HR user, and are unavailable to the Linux Docker container;
do not use the demo account system or this integration as production access
control. Revoke access through the Google account if the connection is no longer
needed. The local demonstration is not a production deployment.
