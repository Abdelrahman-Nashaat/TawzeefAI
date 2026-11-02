# Optional local automatic provisional screening

Uploads always create durable pending review entries. To enable the local worker for a Windows demo process:

```powershell
$env:TAWZEEF_AUTO_PROVISIONAL = '1'
& .\start-demo.ps1
```

Clear the process environment variable to disable it for subsequent starts. All processes sharing the same SQLite database coordinate claims using database leases; at most two unexpired claims are active. The queue reports whether any worker has sent a recent heartbeat, which is not proof that every pending case will be processed. An expired claim can be reclaimed after a crash; retries are bounded, and failed jobs remain visible for HR to handle.

The worker uses local deterministic keyword matching only. It stores a separate result attributed to `system_worker`, not to a candidate or HR, and never advances or rejects an application, sends a message, or calls an external provider. A score is a provisional mention count, not verified ability. Automatic masking may miss names or other identifiers. A result can only contribute to prioritization after the masking was reviewed and the result is current. HR must refresh or review manually if the result becomes stale after any source, job, assignment or mask change, or when the worker reports superseded, failed or manual; these states do not reject the application.

The worker supports at most 4,000 resume characters and 20 combined job skills, each up to 80 characters. Longer inputs are not truncated or scored; they remain for manual review. Historical pending applications are discovered incrementally while the worker runs. Keep demo data synthetic and do not expose the local server on a public network.
