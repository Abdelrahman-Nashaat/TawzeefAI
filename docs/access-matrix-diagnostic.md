# Synthetic API access matrix diagnostic

Run from `backend` with the locked development dependencies:

```powershell
uv run --locked python evaluate_access_matrix.py
uv run --locked pytest -q tests/test_evaluate_access_matrix.py
```

The runner uses a temporary SQLite database, fictional accounts, a synthetic résumé,
a private conversation and an HR-only fictional policy. It disables the configured
external AI, hybrid and Google integrations and creates no real messages. It checks
unauthenticated access; candidate, employee and HR dashboard access; two candidate
ownership boundaries; HR-only masking and screening; private conversation access;
and role-filtered policy retrieval. HR responses containing the marker and an owner
conversation are positive controls. For privacy the JSON reports status and marker
presence, not response bodies or credentials. It reports HTTP 5xx or request errors
as errors and separately counts failed expectations. Setup errors stop the run and
are not reported as success.

This fixed, small set exercises real local API routes and returns exact HTTP results.
Its checks do not establish zero data leaks, cover all routes or races, evaluate the
correctness of policy answers, or establish production security. Run it on a pinned
commit and record the commit, run date, configuration and all failures before citing
any results. Policy prompts intentionally omit the private marker so an echoed
question cannot masquerade as a leak or positive control.
