# TawzeefAI evaluation plan

This plan translates the capstone booklet into checks for a working recruitment assistant. The booklet's 8-of-10 ranking target, policy-answer correctness, no cross-role leakage, and reduction in screening time are **suggested success metrics**. A passing test suite is evidence about the cases exercised, not a guarantee of fairness or security.

## Data and reference answers

Use a small, versioned, synthetic dataset that contains no real candidate or employee information. Clearly label every policy and job description as a **sample** until an authorized organization supplies representative documents. Do not describe invented examples as an actual company's policy or historical hiring outcomes. Separately record the source and permission for any real documents later supplied; redact personal data before using them in tests.

Prepare:

1. A representative job description with explicit job-related skills, experience, and requirements, including a defined scoring rubric. Add a similar but distinct role to expose retrieval and matching confusion.
2. At least ten synthetic résumé and role test cases, with independent expected ordering or pairwise comparisons documented **before** running the system. Include strong and weak matches, close matches, transferable skills, missing experience dates, and ambiguity. If a requirement is absent or unclear, distinguish missing evidence from a proven lack of skill.
3. A sample employee handbook with short policies, version and section identifiers, and at least one policy restricted to staff. Prepare policy questions with expected answers and supporting document sections, plus out-of-scope and unsupported questions that require an explicit unknown response.
4. A clearly labeled fictional, anonymized archive of successful-hire **patterns**, not individual real hire records. Treat past hiring patterns as potentially biased; do not make them an automatic eligibility rule or present them as evidence of a person's suitability.
5. Test identities for candidate A, candidate B, employee, and HR, and interview availability in explicit time zones. Seed test dates for candidates at different pipeline stages, including no hire date and no applications.

Cover English and Arabic when these are supported, text PDF and DOCX, blank or corrupt files, duplicate uploads, long and multi-column files, scanned PDFs, and hostile instructions embedded in a résumé. Record which formats and languages actually work; do not claim OCR support unless it is exercised. All CVs, policies, questions and calendar events in tests should be synthetic.

## Matching and bias

The [counterfactual screening diagnostic](counterfactual-screening-diagnostic.md) exercises fixed fictional identity variants through the local APIs, including simulated masking. It detects differences in those examples and is not proof of fairness.

- For each test case, record role, rubric version, input, expected result, actual ordering, score breakdown, cited evidence, missing evidence, and reviewer verdict. Report number correct out of ten or more separately from pairwise ranking accuracy. Define what constitutes a correct ranking and how ties are treated in advance; do not tune the rubric on the held-out cases.
- Confirm explanations refer to evidence in the submitted résumé and job requirements. Test unrelated role confusion, skill synonyms, insufficient evidence, and unsupported qualifications. Scores must not be presented as final hiring decisions. Provide a human reviewer with the evidence and ability to override or reject recommendations.
- Evaluate a named risk: a candidate's gender or a name associated with gender influencing ranking despite equivalent job-related qualifications. Create paired synthetic résumés that differ only in the chosen attribute; measure score and ranking changes and review explanations. Treat such tests as a way to detect disparities in these examples, not proof of fairness across populations. Avoid using protected attributes and obvious proxies in scoring; this alone does not guarantee unbiased results.
- Document who set the rubric, what data was used, known limitations, results by relevant test condition, and follow-up for any material discrepancy. Do not use personal attributes inferred from a résumé as ground truth.

## RAG evaluation

- For policy and role queries, identify the expected supporting section before running retrieval. Calculate retrieval recall@k as the fraction of questions with at least one expected section in the top k; state k, dataset size, and indexing configuration. Test similar roles, access-restricted sections, and absent content.
- Evaluate answers separately for correctness, whether the supporting source actually supports the claim, correct citations, and whether the system abstains when evidence is absent. Include exact expected references and human-reviewed judgments. An LLM-based judge, if used, is supplementary and must not be the only evaluator.
- Submit instructions inside synthetic policy and résumé documents that try to override rules or expose another user's data. Check they are treated as untrusted content, do not grant new authority, and do not trigger unauthorized tool actions.

## Authorization, privacy, and audit

Test through actual server endpoints and storage/retrieval boundaries, not only through hidden UI elements. For each role and resource, record expected allow/deny and observed status. Include unauthenticated requests; candidate A requesting candidate B's records, résumé, conversation and status by changing IDs; employee access to HR-only resources; and a candidate trying to call HR actions. Confirm retrieval and search results obey the same access rules as direct reads. Inspect responses and citations for protected data and metadata. Use negative tests as well as positive tests.

For screening and status changes, verify that a durable audit entry captures actor, event, time, target, rubric/version and decision or override, with sufficient evidence to reconstruct the action. Check failed and denied attempts where appropriate. Never put secrets or complete résumé contents into routine logs; minimize test data in traces and error messages. Test that users without permission cannot view or modify audit records.

For keyword screening, audit events record the method, score, match counts and a SHA-256 digest of the exact serialized result without copying résumé excerpts into the audit row. New screenings also retain the exact result in a separate, HR-only history; reads compare it to the audit digest before showing it. The snapshot can contain partially redacted résumé excerpts and identifying information, and remains in the local database until the database is removed. The digest is an integrity check, not protection against an actor who can change both the snapshot and audit row. Earlier entries without snapshots cannot be reconstructed, and previous audit rows are not rewritten or scrubbed. Verify HR authorization, revoked sessions, pagination, integrity failures and atomic rollback for both direct and workflow screening. This retained history does not verify the result or establish a human hiring decision.

Test secret handling using dummy values: no secrets committed to the repository, exposed in browser responses, or printed in logs. Test expiration, invalid sessions, and role checks. The booklet's proposed zero-leak target applies to the tested cases; no finite suite proves the absence of all possible leaks.

## Workflow, scheduling, and dashboard

- Exercise a multi-step workflow from application through screening, human review, interview scheduling, and status updates. Verify persistence across interactions and that each action is associated with the correct candidate. Check permitted and invalid status transitions.
- Test conflicting slots, time zones including daylight-saving transitions, unavailable slots, authorization, failure/retry, and duplicate request handling. A local scheduling simulator can test application behavior; it does not establish that a live calendar integration or invitation delivery works.
- Calculate pipeline counts and screening volume from known seeded records. Define time-to-hire precisely, including start and end events, units, and which records are excluded. Test no records, missing dates, and date boundaries. Compare displayed metrics against independently calculated expected values.
- Validate accessible role-appropriate user journeys and basic error handling for missing or malformed input. Test that any destructive or externally visible action is actually authorized and that errors are understandable.

## Timing evaluation

The [synthetic timing-study procedure](screening-time-study.md) prepares a frozen, counterbalanced study packet and reports only observations actually recorded by reviewers.

Measure manual baseline and assisted screening with human reviewers using the same rubric and comparable cases. Record participant count, case order, training, inclusion criteria, timestamps, per-case time, outcomes, and exceptions. Counterbalance the order or use equivalent cases to reduce learning effects. Report per-case and aggregate times, quality alongside speed, method and limitations. Do not substitute program execution time, simulated human timings, or an unmeasured claim for the booklet's requested real comparison. External calendar delivery, cloud service behavior, and real-user measurements remain unverified until those integrations and users exist.

## Release evidence

Keep a dated result table with the scenario, dataset/rubric version, command or manual procedure, expected result, observed result, pass/fail or not tested, and issue link or remediation. Include the number of cases and false-positive/false-negative examples where useful. Store only synthetic test data in the repository. Re-run affected checks after changes and document remaining limitations; never mark unrun checks as passing.
