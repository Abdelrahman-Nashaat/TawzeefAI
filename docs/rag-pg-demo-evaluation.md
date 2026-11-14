# PostgreSQL demo knowledge diagnostic

`python -m app.rag_pg_demo_evaluation` tests the authenticated experimental
PostgreSQL demo endpoint, separately from the SQLite-backed retrieval diagnostic.
It loads a small fictional development set, compares the default lexical and
experimental hybrid responses, and reports expected source/page in the first
three excerpts and in semantic-only suggestions. It also checks no-support cases,
HTTP errors, role access, collection/job/section filters, and whether returned passages belong to the cited page.
Passages embedded by the local model are checked against chunks of restricted
fixture pages, including pages outside the requested filters, using the same passage splitter as the endpoint. This check can
flag shared text that is also present in an authorized document. It observes only
passages newly passed to the embedding model; cache hits are not observed by this
check. The lexical mode does not call the embedding model. Returned excerpts and
suggestions are checked independently in both modes.
This measures retrieval on a small known synthetic dataset, not factual answer
correctness, a held-out benchmark, fairness, or production authorization safety.

Use an operator-provisioned disposable PostgreSQL database whose name matches
`tawzeef_test_<32 lowercase hexadecimal characters>` and contains pgvector. The
process checks that name and creates a random experimental schema, loads only
fictional data, and drops that schema on exit. It does not delete the database.
Startup requires a new schema and will fail if the name already exists; cleanup
only targets the schema created by this run and checks its database identifier.
Never point the diagnostic at a database containing data that must be retained.
Use local-only access and remove the disposable database when finished. From
`backend`, configure a DSN without putting credentials in a shared shell history,
and run with the PostgreSQL and semantic extras installed:

```powershell
$env:TAWZEEF_TEST_POSTGRES = '1'
# Set TAWZEEF_TEST_POSTGRES_DSN securely for the disposable test database.
uv run --locked --extra postgres --extra semantic python -m app.rag_pg_demo_evaluation --model-dir ../.tooling/multilingual-e5-small
Remove-Item Env:TAWZEEF_TEST_POSTGRES
```

The model and tokenizer are checked against pinned hashes before use. No external
model service is called. The JSON contains synthetic source excerpts and detailed
case results; review it before sharing. A nonzero exit indicates an HTTP, access,
filter, or citation violation, not a guarantee of quality when zero. A missing, unrelated,
or misleading source is still possible even when its page and permissions pass.

## Recorded development run

On 2026-11-13 at 05:01 UTC, the diagnostic ran on the 13 fictional questions
using PostgreSQL 16.15, pgvector 0.6.0, and the pinned E5 model. The full
[synthetic result](rag-pg-demo-evaluation-results.json) records source excerpts,
queries, timings, and runtime and dataset hashes. It reflects the earlier dataset and runtime hashes, before job-specific cases and filter checks were added; rerun for the current version. This is a small non-held-out diagnostic.

Both lexical and hybrid modes returned the expected document and physical page
as an excerpt for **5 of 7** answerable questions, and both abstained for all
**6** questions with no accessible supporting document. Hybrid additionally
returned the expected page as an unverified suggestion in each of the other two
answerable cases. One unsupported parental-leave query received unverified
suggestions for unrelated passages, despite abstaining. No access, citation,
or HTTP violation was detected in these cases. None of these observations proves
that the displayed excerpts answer their questions or that the system is safe for
real recruitment data.

## Expanded development run

On 2026-11-14 at 03:10 UTC, the revised diagnostic ran against a disposable
PostgreSQL database with pgvector and the pinned local E5 model. The
[recorded results](rag-pg-demo-evaluation-results-v2.json) identify the dataset
and runtime by hash. This run preceded the separate duplicate-review route
integration and does not represent a run on the current repository HEAD. The
19-question fixture includes similar job descriptions,
section and job filters, and HR-only past-hire patterns. In both modes, all four
new answerable job and pattern cases returned their expected first excerpt, and
both new unsupported cases abstained. Across all questions, both modes returned
expected excerpts for 9 of 11 answerable cases and abstained for all 8 unsupported
cases. Hybrid produced suggestions for one unsupported question. No access,
filter, citation, or HTTP violations were detected. These results are synthetic,
not a held-out benchmark or proof that generated answers are correct.
