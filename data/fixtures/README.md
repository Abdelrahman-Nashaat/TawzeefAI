# Synthetic TawzeefAI fixtures

All names, organizations, policies, job postings, résumés, and hiring patterns here are invented for development. They are not actual people, company policies, historical hires, or externally reviewed ground truth. The proposed judgments are illustrative and require independent review by a human subject-matter expert before they are used to make claims about hiring quality.

## Files and stable identifiers

- `jobs.json`: fictional job postings. A `job-` ID identifies a role; `required_skills`, `preferred_skills` and `responsibilities` are plain-text evidence to match against, not a mandated scoring algorithm.
- `candidates.json`: fictional candidates, with `cand-` IDs and concise résumé text. `tags` describe test conditions, not protected attributes for scoring. `counterfactual_pair` identifies an artificial name-only comparison; it is test metadata and must never be exposed to the matching model as a candidate qualification.
- `ranking-cases.json`: `rank-` cases with job and candidate IDs, expected ordering for the stated rationale, and limitations. Expected outcomes are proposed test labels, not results from the application or human-approved hiring recommendations. An outcome of `insufficient_evidence` requires an explicit uncertainty indication rather than ranking.
- `policies.json`: fictional policy sections (`pol-`), their access audiences, and fictional anonymized hiring-pattern descriptions (`pattern-`). `public` means candidate, employee, and HR; `staff` means employee and HR; `hr` means HR only. These values are fixture conventions, not an implementation of access control.
- `policy-questions.json`: `qa-` cases with role, question, expected supporting section ID or abstention/denial. `required_facts` are reference facts to assess, not ready-made answers that the app should return without retrieval.

All IDs are stable strings. File references use IDs rather than array offsets. Dates are ISO 8601 where present; these fixtures have no contact details, actual birth dates, or protected attributes. Names in a paired test differ intentionally while job-relevant content stays identical. Use separate runtime identities and server-side authorization to evaluate access control.

Do not use any of these cases as the sole basis for model selection, production policy, deployment, claims about fairness, or actual hiring decisions. Test OCR separately with appropriately licensed synthetic image fixtures; no scanned documents are included here.
