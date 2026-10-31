# Policy synonym diagnostic

This is a newly authored synthetic single-passage diagnostic, **not** an
independently human-reviewed or blinded benchmark. Its six supported and eight
unsupported pair labels are provisional. See `PAIRS` in
`backend/app/policy_synonym_diagnostic.py` for every question, passage, and
expected label. The pairs were written during development and have not been
used to calibrate a threshold. The 0.85 cutoff was previously explored in the
local reranker diagnostic; it is not a validated support threshold.

Run locally with the pinned E5 files installed as described in
`local-semantic-experiment.md` and the semantic dependencies available:

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.policy_synonym_diagnostic
.\.venv\Scripts\python.exe -m pytest tests/test_policy_synonym_diagnostic.py -q
```

One local run on 31 October 2026 returned:

| Method | Supported passages returned | Unsupported passages returned |
| --- | --- | --- |
| Existing lexical eligibility gate | 0/6 | 0/8 |
| E5 cosine >= 0.85 plus existing qualifier check | 1/6 | 0/8 |

The one additional candidate was `en-remote`. The closest unsupported case,
`ar-remote-nopay`, scored 0.845897, close to the cutoff. Exact per-case scores
are reproduced by the command and can differ slightly across runtimes. The
synthetic labels, narrow sample and closely overlapping scores make this result
insufficient to justify enabling the experimental gate. No production policy
retrieval behavior is changed by this diagnostic.

This tests one question against one passage at a time. It does not measure
PostgreSQL search, ranking across passages, RBAC, source revocation, generated
answer grounding, broad synonym coverage, or timing in production. Before any
integration, independently label and freeze a larger multilingual set, assess
near-miss policies and authorization, and choose/calibrate a support gate using
separate development data. Citation IDs and embedding similarity alone do not
establish that a passage supports an answer.
