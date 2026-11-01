# Model evidence-relation diagnostic

This is a small development diagnostic of the opt-in model evidence preview,
not an independent test of hiring accuracy or fairness. The twelve excerpts,
criteria, and proposed relation labels are fictional and were authored during
development; a human has not independently validated the labels. They cover
explicit claims, negation, aspirations, attribution to another person,
unrelated evidence, negation scope, and an embedded instruction.

On 2 November 2026, a single batch of the twelve synthetic pairs was sent to
the configured Groq model via the same proposal request function. The returned
relations matched 11 of the 12 draft labels. The discrepancy was `team_scope`:
the model returned `unclear` for a Python service reviewed by a colleague,
where the draft label was `supporting`. This count is not a measure of hiring
accuracy. Repeated runs may differ, and this direct request does not exercise
the full authenticated HTTP endpoint, authorization rechecks, or the effect of
redaction on a real résumé.

The local preview attaches limited source-linked cues for possible negation,
aspiration, or reference to another person. These cues are heuristic; a cue
may refer to a different skill or clause, and missing cues do not establish
support. The preview also flags an internally inconsistent `supporting` label
paired with a `negated`, `aspiration`, or `other_person` reason. It does not
change the model's suggestion, an HR judgment, a score, or a hiring decision.

To repeat, from `backend`, provide `GROQ_API_KEY` securely in the process
environment and run `python evaluate_evidence_proposals.py`. The script sends
only the fixed synthetic examples in that file. It reports case IDs, draft and
model relations, and reason codes; it does not print the key or provider error
bodies. Provider failure or invalid output is reported as unavailable rather
than a successful evaluation. A larger independent, multilingual, blinded and
human-reviewed set is required before drawing conclusions about performance.
