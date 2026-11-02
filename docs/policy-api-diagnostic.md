# Policy conversation API: synthetic retrieval diagnostic

This diagnostic runs the **actual policy conversation API**, with a temporary
SQLite database and synthetic handbook documents. The 30 questions and four
negative cases have draft labels generated with the data; they have not been
independently reviewed by HR. The corpus was already available during project
development and is not a held-out benchmark. Retrieval of the expected source
does not establish that an answer is correct, grounded, fair, or safe.

## Later run with Arabic spelling leads

On 2 November 2026 the same command returned a draft-expected source in the
first three for 17 of 30 questions. Fourteen responses were extractive and 16
abstained. Among 15 Arabic questions, six returned the expected source in an
extractive response and three more returned it only as an explicitly
unverified lead while abstaining; six missed it. The three lead cases were
`cap-qa-12`, `cap-qa-16`, and `cap-qa-22`. All four negative cases abstained
without their listed forbidden source, and one revoked source was hidden.
Those small checks do not prove general access control or answer quality.
The earlier run below predates both the negation guard and spelling leads, so
the two runs are not a controlled comparison of the spelling change alone.
The labels are development drafts rather than an independent evaluation.

The runner imports each logical handbook section as one text document, rather
than a PDF, so it does not measure PDF parsing or real pagination. It disables
Groq and hybrid search; the result describes the local lexical policy path only.
It tests one explicit source deactivation through the API; it does not prove
authorization under every possible race, role change, or document change.

Run from `backend` with the project's dependencies installed:

```powershell
.\.venv\Scripts\python.exe evaluate_policy_api.py
.\.venv\Scripts\python.exe -m pytest tests/test_evaluate_policy_api.py -q
```

On 1 November 2026 the command exited successfully with these results:

| Measure | Observed |
| --- | --- |
| Draft expected source in first three, all questions | 15/30 |
| English | 9/15 |
| Arabic | 6/15 |
| Response mode | 15 extractive; 15 abstained |
| Negative cases abstained | 4/4 |
| Negative cases returning a forbidden source | 0/4 |
| Previously displayed source hidden after deactivation | yes, one case |

The 15 questions without a returned source were `cap-qa-01`, `04`, `05`,
`11`–`14`, `16`, `18`, `20`–`22`, `25`, `26`, and `28`. These results reveal
substantial missed retrieval on this set, not an answer-accuracy score. The
four negative cases are too few to establish security or general abstention
quality. No generative answer was tested.

Dataset SHA-256 values from the run:

- `handbook.json`: `b73e20e32880980615335ae83d637ae07b5383d67ac2e0cc3df19277c513b375`
- `policy-qa.json`: `e81f0f9b70b215d27228e8ddbd99f148a8fdef55b69dac3d2d15089951deba00`
- `negative-qa.json`: `887a2798d3188df53b51fddfb8afec90d304a6e7599a9e358a28c7fe1563b27e`

An independent review of expected answers and evidence, a held-out corpus,
generated-answer assessment, and human timing baseline are still needed for
the project's proposed evaluation. The runner reports individual IDs and
sources so that misses can be examined rather than hidden in an aggregate.
