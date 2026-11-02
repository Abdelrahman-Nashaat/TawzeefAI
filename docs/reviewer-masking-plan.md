# Reviewer-selected résumé masking: integration plan

**Status: infrastructure only.** `app/resume_mask.py` and its tests are not wired into `app.main`; no upload, screening, assessment, graph, or existing API uses the new mask. The application still uses its existing incomplete rule-based redaction. Do not describe screening as reviewer-masked or blind until the integration and verification below are complete.

A synthetic counterfactual demonstrates the gap: with a job requiring Julia, an unlabelled `Julia Smith` line can be counted as a skill mention, while changing only that name to `Alex Smith` yields no such mention. This example detects one failure, not demographic fairness generally. Guessing and removing the first line could instead erase genuine job evidence.

## Infrastructure contract

The proposed HR review starts from the rule-redacted résumé. HR explicitly selects zero or more additional spans as Unicode code-point offsets; spans are replaced by spaces while newlines and character positions are retained. Selections and revisions are stored without the selected text. The helper returns:

- `text` and `source_sha256`: the masked text and its actual SHA-256.
- `source_revision` and `mask_revision`: monotonic values that change on each raw résumé edit or saved review, including an empty mask.
- `context_sha256`: a separate revision-bound context token, so changing and reverting the source or mask cannot revive earlier decisions. A legacy application with no edits and no mask retains the original text hash as its context token.
- `status`: `unreviewed` for the old rule-only behavior or `reviewed` for an explicitly saved mask. Neither status establishes anonymity or fair decisions.

The save operation checks the displayed source hash and both revisions within an immediate write transaction, validates nonoverlapping ranges, and writes the review and metadata-only audit together. Invalid, stale, or internally inconsistent saved mask state fails closed. The review endpoint may show the current rule-redacted base source to HR to repair a stale selection; this is an editor preview, not evidence that the mask is current. Raw résumé data and historical screening snapshots are not deleted.

## Required atomic integration

Initialize the mask schema and trigger after the application and rubric tables exist. Only expose the HR endpoints after all of the following consumers switch together to the same transaction-bound `masked_source()` result. Preserve the existing unreviewed path for applications without a saved mask and label it accurately; never treat an absent or stale selection as reviewed blinding.

1. **Keyword and parsed views:** Use the selected source consistently in `recruitment.py` screening and structured résumé view, `candidate_screening.py` preview, `workflow.py`, and `graph_orchestrator.py` parser, matching, screening snapshot, preparation, and display. Keep previous stored screening results and audit entries as historical records, clearly separate from a current result.
2. **Fields, evidence, and assessments:** Switch `resume_fields_api.py`, `resume_field_review.py`, `rubric_admin.py`, `rubric_evidence_bundle.py`, `rubric_comparison.py`, and `matching_specialist.py`. Existing evidence and field-review offsets and snapshots must carry a revision-bound context token, with migration behavior that preserves old records but marks them stale after any mask change. Recreating an identical-looking mask must not resurrect old evidence or assessments. A `source_sha256` field must continue to mean the hash of the actual displayed text; use a distinct context field for revision binding.
3. **Semantic and model flows:** Update `graph_semantic.py` saved references and restore checks, the semantic suggestion path in `rubric_admin.py`, `model_evidence_proposal.py`, and any downstream evidence bundle. Check authorization, source, criteria, and mask context before and after model or embedding work, including a change-and-revert while the call is running. Do not expose selected removed spans in checkpoints, model requests, audit messages, or derived suggestions.
4. **Planning and approval:** Include the mask context in `adaptive_planner.context_for()` and every graph parser/checkpoint/action fingerprint used by `graph_orchestrator.py`. A stale selection or any intervening mask edit must prevent approval, including an edit followed by a revert. Recalculate the proposal from current authorized data, inside the existing transaction, before committing any screening or advancement.
5. **Candidate release:** When a mask is saved, bump `rubric_publication_revisions` and invalidate the current `rubric_publications` release in the same transaction. A previously shared assessment must remain unavailable until HR reviews and shares a current assessment again. Historical audits and screening history remain unchanged; a failed mask write must not invalidate a release or partially update any revision.
6. **User interface and startup:** Add the HR-only review editor in `frontend/app.js`, convert browser UTF-16 selection offsets to Unicode code points correctly, and display a current/stale/unreviewed state and the limits of masking. Wire schema initialization and the router in `main.py` only when the complete transition is ready. Handle source changes, conflicting writers, and stale responses visibly.

## Acceptance checks before exposure

- API tests for HR-only read/write, account and session revocation, invalid/overlapping/out-of-range spans, concurrent saves, atomic audit and publication invalidation, and rollback on failures.
- Unicode tests with Arabic, emoji, combining marks, and multiple lines, including browser selection to server offsets. Reapplying identical selections and changing/reverting raw source must still invalidate older context.
- End-to-end synthetic Julia/name counterfactual through upload, HR selection, keyword scoring, candidate preview, workflow, and graph. Confirm all use the selected source; test an ordinary skill-only first line so it is not silently discarded.
- Existing evidence, assessments, field reviews, semantic suggestions, pending graph approvals, and candidate releases become stale on mask save, on a later edit, and after a change-and-revert. Check transitions while a model call is in flight and ensure no stale masked text is sent after authorization is revoked.
- Confirm preexisting screening history and audits remain accessible as historical records without being presented as current, and that users without an HR role cannot read or edit the review.
- Browser verification for selecting, saving, revising, and recovering a stale mask; run affected suites for recruitment, rubric, graph, semantic and candidate release, plus lint and JavaScript checks. The feature is an aid to review, not proof of anonymity, fairness, or verified qualifications.
