# Google Calendar and Gmail integration: development status

This is an unfinished local integration. OAuth credentials can be authorized on
Windows, and provider calls and a durable delivery outbox are covered by tests
with fake Google responses. An explicitly enabled Windows host can configure a
live dispatcher using the current Windows user's credentials. A private synthetic
Calendar event was created and removed with the service adapter on 30 October
2026. A subsequent synthetic private event without attendees was created and
removed through the local UI and Google Calendar; Google reported it cancelled.
Real Gmail delivery has not yet
been tested end-to-end.

The backend has an experimental, explicit Calendar removal API for newly
confirmed deliveries with saved event version and credential binding. It checks
the original identity, private event marker, content, and version before a
conditional delete; failures and lost results block local cancellation. An old
unbound delivery, an interrupted or ambiguous send, and an ambiguous deletion
remain unresolved. Removing an event is a separate explicit HR operation; it
does not itself cancel the local interview or notify a recipient.

HR can explicitly reprepare a definitely rejected delivery as a new local draft;
the rejected attempt remains recorded, and sending or correcting the new draft
requires its current generation. Repreparing does not contact Google. Interrupted
or ambiguous delivery still needs external reconciliation. For an interview with
a confirmed earlier email, HR can separately preview and explicitly approve a
cancellation notice to the same recipient after local cancellation. The app
cannot verify that the currently authorized Gmail account sent the earlier
confirmation; HR must check. An interrupted or uncertain attempt requires HR
to verify Gmail and attest the outcome before another attempt. This attestation
is not provider proof; all processes using the local SQLite database must share
its canonical cancellation-notice lock file. No cancellation notice is sent
automatically, and the cancellation email has not been tested live. Never retry an ambiguous Gmail send
automatically: the first attempt may have succeeded. Google may send some
notifications even when an event write requests `sendUpdates=none`.

For opt-in host configuration, `app.google_dispatch.prepare_dispatch`
and `app.google_dispatch.prepare_calendar_removal` return request-local actions
bound to a fingerprint derived from the client ID and refresh token. Changing
credentials causes old events to fail closed; it does not prove the Google
account changed. Both factories are dormant unless the Windows host opts in. The
`POST /api/google/deliveries/{id}/calendar-removal` endpoint requires HR access
and an explicit confirmation, and does not automatically cancel the local
interview after deleting the provider event.

## Preparing a test account

The account owner needs a Google Cloud project with Google Calendar API and
Gmail API enabled, an OAuth consent configuration, and an OAuth client of type
Desktop app. Add the intended Google account as a test user if the consent
configuration requires it. Download its client JSON to a private local location
outside the repository; do not paste the file contents into chat or commit it.
The app's OAuth client is separate from any connected Gmail or Calendar integration.

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
not enable the app's live dispatcher. Start `start-demo.ps1 -Google` from the
repository root to enable it on the Windows host; normal startup and Docker
remain disabled. Explicit HR actions may create events or send messages when
the provider is enabled. The credentials are shared by the local
application, not per HR user, and are unavailable to the Linux Docker container;
do not use the demo account system or this integration as production access
control. Revoke access through the Google account if the connection is no longer
needed. The local demonstration is not a production deployment.
