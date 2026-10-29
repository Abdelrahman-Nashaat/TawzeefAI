# TawzeefAI implementation plan

The source of requirements is `requirements.md`; this plan proposes an implementation rather than changing those requirements. Deliver a local graduation demonstration first, with explicit boundaries around real external services and personal data. Use synthetic data for development.

## Work sequence

1. **Foundation** — locked Python dependencies, localhost-only server, a persistent local database, secure password storage, revocable server-side sessions, server-enforced roles, and reproducible startup.
2. **Recruitment** — jobs and candidates, upload and extraction with explicit unsupported/scanned-document handling, evidence-based matching, human review, persisted application state and an audit trail.
3. **Knowledge and agent** — role-filtered retrieval over policies and job descriptions, citations and explicit uncertainty, persisted workflow steps and tools. A configured model must be exercised for the generative flow; an extractive fallback is labeled clearly.
4. **Interview and dashboard** — internal appointment scheduling with conflict checks, persisted event history, actual screening volume, pipeline and time-to-hire calculations. External calendar integration is described only when actually configured and verified.
5. **Interfaces** — usable, accessible HR, candidate and employee experiences backed by the actual API and permissions, with responsive browser testing.
6. **Evaluation and delivery** — independent synthetic test cases, behavior and permission tests, provider integration checks when credentials exist, documented limitations, and operating instructions. Compare observed outcomes with the booklet's suggested measures; do not fabricate a manual baseline or claim broad fairness from a small sample.

## Boundaries

- An HR reviewer makes hiring decisions; the system supplies evidence and records review. Sensitive or protected traits must not be used to score a person.
- Only return authorized data and retrieve from authorized documents; a filtered interface is not a substitute for server-side checks.
- Résumés and retrieved text are untrusted input, never instructions to change tool permissions or expose data.
- Do not put secrets in source files or logs. Model access and external services require actual configuration and a successful connection test.
- Local development configuration and demo credentials are not a production deployment. Real recruitment data requires a separate operational security and privacy assessment.
