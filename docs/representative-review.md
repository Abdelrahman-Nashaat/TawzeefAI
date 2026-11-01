# Representative-role human review

This procedure prepares a human review of ten synthetic résumé comparisons for
the representative role in `data/representative_role/`. It freezes the actual
`score_resume` keyword-mention outputs as a **baseline**, not the human-reviewed
weighted rubric or an automated hiring decision. The profiles and role summary
are fictional or paraphrased test data. This procedure does not create a human
judgment: a real independent reviewer must supply and attest to one.

From `backend`, choose two new output paths outside the repository:

```powershell
uv run --locked python review_representative_role.py prepare --packet C:\temp\review-packet.json --manifest C:\temp\review-manifest.json
```

Create the output directory first. Neither file may exist, and the two paths
must differ. The packet contains a job summary and shuffled, pseudonymized
pairs, without draft expected rankings, system scores, or the case mapping. The
manifest contains the mappings and frozen full outputs; keep it away from the
reviewer until review is complete. Names are not sent in the packet, but the
synthetic source profiles are public: this is practical blinding for a reviewer
who has not examined the source dataset, not a guarantee of anonymity or
independence. Do not use the optional random seed in a real review if it has
been shared with the reviewer.

The reviewer fills in their name and independence attestation, then chooses `A`,
`B`, `tie`, or `uncertain` and supplies a rationale for every pair. Review
requires human judgment of job-related evidence; a choice is not a hiring
decision. The reviewer should not inspect the manifest, source labels, or system
results during review. Do not edit the role, profiles, pair IDs, or instructions.

After review, use the same source files and code version:

```powershell
uv run --locked python review_representative_role.py report --packet C:\temp\review-packet.json --manifest C:\temp\review-manifest.json
```

The report compares the reviewer's pairwise choices with the frozen keyword
scores. Ties are counted explicitly; uncertain judgments are excluded from the
agreement denominator and reported separately. The report does not use the
dataset's draft expected rankings. It records reviewer identity, the counts and
the frozen manifest fingerprint, but does not independently verify reviewer
identity or independence. The fingerprints detect accidental changes rather
than malicious forgery. The script rejects changed packets, datasets, scorer,
blinding code or evaluation script; use a new study for changed inputs.

Multiple reviewers, an independently curated held-out set, inter-reviewer
agreement and a manual time study are still needed for stronger conclusions.
Do not describe a generated packet, a test fixture, or agreement with one
reviewer as validated hiring accuracy, fairness, or time savings.
