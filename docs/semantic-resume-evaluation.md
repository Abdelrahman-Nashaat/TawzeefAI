# Résumé evidence retrieval: synthetic diagnostic set

`data/fixtures/semantic-resume-cases.json` specifies 14 fictional examples and
reference spans written without consulting model output: English, Arabic, a
cross-language example, a related role, negation, aspiration, attribution
to another person, and a name/gender counterfactual pair. It is a **development set**, not actual candidates, a real
representative role, or an independently reviewed held-out benchmark. Do not
quote its results as evidence of hiring performance or fairness.

## Procedure

Run the synthetic set against the local HR endpoint from `backend` using the
real pinned local model:

```powershell
uv run --locked --extra semantic python -m app.evaluate_semantic_screening
```

The script uses a temporary database with fictional jobs and DOCX uploads;
it prints one JSON report with individual cases, returns, latency and metrics.
Set `--model-dir` only when the pinned model is located elsewhere. The report
records the current Git commit and whether the worktree was dirty; retain the
diff for an exact code snapshot if dirty. The model loader verifies the recorded
expected hashes; an unavailable model is recorded as an HTTP error per case.

1. Record the dataset version, model and tokenizer hashes, model settings,
   scoring or rank threshold, returned limit `k`, code revision, platform, and
   date before running. Keep the same settings across all cases and report any
   skipped case or error. Do not tune thresholds against a final evaluation set.
2. For each case, feed only its fictional résumé and criterion through the
   actual HR-authorized retrieval path. Record the ordered returned spans,
   similarity values, no-result indication, latency and errors. Validate each
   returned text against its declared source and Unicode code-point offsets in
   the exact partially redacted text; offsets do not refer to the uploaded file.
3. Compute **support recall@k** for cases labeled `support`: a hit means at
   least one returned source span contains a complete listed supporting span.
   Report hits and denominator, overall and by language; for partial overlap
   use human review rather than silently counting a hit.
4. Separately report the no-result rate on `no_support` cases, and list every
   returned unrelated span as a false candidate. For `negated` and `unconfirmed`
   cases, record whether qualifying context is retrieved and whether any UI or
   downstream score treats it as proof. Retrieving a negation is not itself an
   error; presenting it as evidence that the criterion is met is an error.
   A no-result response must mean only that no candidate was returned, never
   that the applicant lacks the skill.
5. Independently inspect all outputs for role restrictions, current-session and
   source rechecks, incomplete identity masking, and network or log disclosure.
   Confirm no model response automatically marks a criterion met/missing,
   changes a score, rejects a candidate, or sends a message. Include synthetic
   role-revocation and source-change cases in endpoint tests.
6. Compare the two `identity-pair-1` results. After masking the two labelled
   name and gender fields, their remaining inputs are identical: check that
   returned evidence, ranking and scores are the same. A failure reveals a
   problem in this pair; success does not establish general fairness.

Before any real use, define and freeze a larger held-out set for a real role
with authorization, documented relevance labels and blinded human review. Include
similar roles, realistic document layouts, Arabic and English variants,
counterfactual names/gender, uncertainties and negative statements. Report
sample size, annotator agreement and disagreements, per-group results, failure
examples and the manual baseline separately. A small synthetic set does not
establish absence of discrimination, leakage, factual errors or performance
on real applicants.
