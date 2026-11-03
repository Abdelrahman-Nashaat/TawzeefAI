# Human-observed synthetic screening time study

This protocol prepares a reproducible practice study for the presentation's request to compare manual and assisted screening on 30 CVs. It does **not** produce or claim a human measurement. The bundled inputs are labeled fictional development samples, not a representative real role or independent quality benchmark. Verify source provenance before use: the program cannot establish that modified source files contain only synthetic information. No participant names or real candidate data belong in these files. Obtain each participant's informed consent, explain what is stored and who can access it, and use only synthetic information; withdraw a participant's data if appropriate under your agreed process. Keep the directory private and outside the repository.

Two consenting reviewers, `R1` and `R2`, each review every one of the 30 different fictional CVs exactly once. Each case is manual for one reviewer and assisted for the other. Both review the same case order; R1 performs 15 manual then 15 assisted cases, and R2 performs 15 assisted then 15 manual cases. A reproducible random seed chooses case order and assignment before timing. This balances the order of conditions and pairs by case, but the two people differ and condition is still confounded with time within each person. Do not treat a two-person pilot as representative, independent proof, a causal estimate, or a substitute for a larger counterbalanced study. Do not let participants see each other's choices or timings until the study finishes.

## Prepare

From `backend`, choose a new directory outside the repository and a preselected integer seed:

```powershell
uv run --locked python screening_time_study.py prepare --directory C:\temp\tawzeef-timing-study --seed 2026
```

The parent directory must already exist and the study directory must not. The command creates a frozen manifest, two participant packets, and two blank CSV observation sheets. Packets contain only the fictional role and résumé text and the assigned condition, not the source dataset's draft reference labels or system results. The manifest stores source hashes and the seed; its digest detects accidental changes, not malicious tampering or a dishonest observer. Complete practice and consistent reviewer training before starting: define in advance the same job-related rubric and outcome meanings for both conditions. Prepare the same synthetic roles and résumés in the local app **before** timing so that routine setup is consistent. Preserve the app version, configuration, rubric, environment, study date, observer procedure, and training description separately; the script does not verify them. Never submit any real CV, policy, contact information, credentials, or private data.

## Observe

Each reviewer separately sets `consent`, `training_complete`, and `independent_review` to `true` or `false` in their packet. Consent and training must be `true` to calculate a report; independence is a self-attestation and a false or absent value is explicitly reported. Do not write names or other personal details into any file. Review each case according to its assigned condition in packet order. In manual trials, use the agreed rubric and source without the assistant. In assisted trials, use the same rubric and the local app's evidence; review output rather than accepting it automatically. Use the same start/stop definition in both conditions: start when the reviewer begins inspecting the prepared case and stop after recording the evidence judgment. Include any tool interaction within that interval, but do not mix setup or unrelated breaks into it.

Enter timezone-aware ISO 8601 timestamps such as `2026-11-02T12:34:56+02:00` in `started_at` and `ended_at`, and select one descriptive `outcome`: `sufficient_evidence`, `insufficient_evidence`, or `uncertain`. These labels are the reviewer's judgment, not verified ground truth or a hiring decision. For a disrupted trial enter `interrupted`, `technical_failure`, `protocol_deviation`, or `other` in `exception`; the observation is excluded even if times were entered. Leave unfinished rows present and blank. Do not enter free-text explanations, names, demographic attributes, or real data in the CSV. Complete both reviewers' sheets separately; do not edit study IDs, case IDs, assignments, or packet materials.

## Report

```powershell
uv run --locked python screening_time_study.py report --directory C:\temp\tawzeef-timing-study
```

The JSON report shows only paired completed cases, excluded observations, medians and paired differences. Blank, interrupted, or otherwise excluded cases are **not** zero-second timings. Invalid assignments, overlapping or out-of-order completed intervals, missing consent, edited materials, and time formats without offsets are rejected. If the order was violated, mark the affected observations as protocol deviations; the tool cannot verify unrecorded events. Negative paired differences mean the assisted observation was slower for that pair. Reviewer outcomes are included only as descriptive context, and do not measure accuracy or establish comparable decision quality. Report completed-pair count, exclusions, participant count, independently assessed quality, protocol deviations, limitations, and the separate data/version record alongside any numbers. The script does not generate confidence intervals, claim a pass, infer fairness, or prove the participants' consent or independence.
