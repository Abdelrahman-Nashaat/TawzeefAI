# Independent review of synthetic policy answers

The existing frozen run contains 30 draft-answerable and four negative policy
questions. Retrieval results do not establish answer correctness. Two people
can independently assess the same recorded outputs using the fictional
handbook and the packet below. The run and policy are development material,
not a held-out benchmark or a real organization's policy.

From `backend`, prepare a new directory outside the repository:

```powershell
uv run --locked python policy_answer_review.py prepare C:\path\to\new-review-directory
```

`materials.json` contains each question, requesting role, answer, mode,
returned source IDs, call-error indicator, and the complete fictional handbook
indexed by the run's document IDs. The runner uploaded handbook sections in
order into a fresh database; preparation checks the run's recorded dataset
hashes and expected-source mappings before reconstructing those IDs. Draft
expected answers are deliberately omitted from the packet. The source text is
synthetic. Neither the script nor the report calls a provider or transmits data.

Give each reviewer the materials and their separate `R1-judgments.csv` or
`R2-judgments.csv`. Ask them to work independently, record their expertise and
the review date outside these files, and avoid seeing one another's judgments
until finished. Leave a field blank when it has not been reviewed. For each
case, compare the answer with the cited source and the rest of the fictional
handbook, including the requester's role and source audience. Enter one value
per dimension:

| Column | Allowed values | Review question |
| --- | --- | --- |
| correctness | `correct`, `partial`, `incorrect`, `uncertain` | Is the answer complete and factually correct for the question? |
| grounding | `supported`, `partial`, `unsupported`, `no_claim`, `uncertain` | Does authorized handbook evidence support the claims? |
| citation | `correct`, `incorrect`, `no_citation`, `not_applicable`, `uncertain` | Do citations identify the supporting, authorized source? |
| abstention | `appropriate`, `inappropriate`, `not_applicable`, `uncertain` | If the system abstained, was that appropriate for the available authorized evidence? |

Use `uncertain` for reviewed cases that cannot be decided, not for unreviewed
cases. Use the notes column to explain errors and why a citation or abstention
was judged inappropriate. Review a provider-error fallback on its actual answer;
record the error separately when interpreting reliability. For no-answer
responses, use `no_claim` and `not_applicable` where appropriate, while still
judging correctness and abstention. Do not credit a source ID alone as proof.

After reviewers return both CSV files to the packet directory, run:

```powershell
uv run --locked python policy_answer_review.py report C:\path\to\new-review-directory
```

The report separates the 30 draft-answerable and four negative cases, counts
reviewed and unreviewed judgments per dimension and reviewer, and lists paired
disagreements. Resolve disagreements separately and retain the initial judgments;
the tool does not adjudicate or infer missing verdicts. Report denominators,
uncertainties, exclusions, provider errors, and both reviewers' outcomes.
Document who reviewed the material and how disagreements were handled before
describing a human-reviewed result. Packet fingerprints detect accidental edits
to materials, not intentional manipulation or reviewer independence. These
development results do not establish real-world answer accuracy, fairness,
security, or performance on a representative held-out dataset.
