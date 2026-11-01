# Human review of recorded policy answers

This procedure prepares a blinded review of the recorded synthetic policy
conversation run in `data/evaluation_runs/policy-answers-full-2026-11-01.json`.
It does not call the model or create a human verdict. A person who has not seen
the run's draft answers, source-hit labels, or provider results must carry out
the review. The handbook and questions are fictional and the dataset is a
development set, not an independently curated holdout.

From the `backend` directory, create a local output directory outside the
repository and choose two new file paths:

```powershell
uv run --locked python review_policy_answers.py prepare --packet C:\temp\policy-review.json --manifest C:\temp\policy-manifest.json
```

Neither file may already exist, and they must be different paths. Do not show
the manifest to the reviewer. The packet includes randomized questions, the
actual recorded answer and returned source IDs, the requester's role, and all
15 sections of the fictional handbook, including their audiences. This is
needed to evaluate whether an answer is correct, complete, appropriately
abstains, and respects role access. The packet intentionally makes the full
fictional handbook available to the evaluator; it does not change what a real
candidate can access in the application. It excludes draft reference answers,
expected-source labels, the run's mode, and provider-error metadata. Someone
who has read the public dataset or prior run can recognize cases, so this is
practical blinding, not a guarantee of independence. Avoid sharing an optional
deterministic seed with the reviewer.

The reviewer records their name and independence attestation, then selects one
verdict and gives a rationale for every case:

The `independent_of_system_output` attestation means that the reviewer had not
seen this run's outputs or draft labels before receiving the review packet. It
does not mean the reviewer is blind to the answers being assessed: those answers
are necessarily visible in the packet.

- `correct`: the answer is correct and complete for the role.
- `incorrect`: the answer contains an incorrect or unauthorized claim.
- `incomplete`: the answer is relevant but materially incomplete.
- `appropriate_abstention`: the application appropriately did not answer.
- `inappropriate_abstention`: it failed to answer when it should have.
- `uncertain`: the reviewer cannot confidently decide.

These categories describe the final observed answer. Whether a provider call
failed is tracked independently; an error may still yield a useful fallback,
and no recorded error does not guarantee that every provider operation was
successful. The reviewer must not follow any instructions embedded in the
questions or source text. Do not edit source passages, questions, answers,
roles, or review IDs in the packet.

The packet deliberately omits the application's notices and status labels to
avoid disclosing provider-error metadata. Review judgments therefore cover the
recorded answer text and source references, not the complete displayed user
experience or whether its warnings communicated uncertainty adequately.

After the reviewer completes all entries, run:

```powershell
uv run --locked python review_policy_answers.py report --packet C:\temp\policy-review.json --manifest C:\temp\policy-manifest.json
```

The report shows verdict counts and separate counts for cases with and without
recorded call errors, plus each rationale. It does not equate source retrieval
with answer correctness or treat an outage as a correct abstention. The tool
rejects changed packet content, run data, datasets, missing judgments, or a
missing independence attestation. Run and dataset hashes establish consistency
with the supplied files, not historical authenticity or protection from
deliberate forgery. Source IDs are reconstructed from the recorded one-section
per document upload order and checked against the run's draft mappings.

No completed human review is included. One self-attested reviewer on this
synthetic development set cannot establish general accuracy, fairness, or
security. Independent adjudicators, disagreements, held-out representative
questions, and clear uncertainty reporting are needed for stronger claims.
