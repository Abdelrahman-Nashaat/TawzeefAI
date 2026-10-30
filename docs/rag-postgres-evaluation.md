# PostgreSQL hybrid retrieval: synthetic diagnostic

`backend/app/rag_postgres_evaluation.py` runs the authenticated experimental
hybrid endpoint with its real pinned local embedding model and PostgreSQL with
pgvector. It uploads no external data, creates an isolated synthetic SQLite
database, and loads only `data/fixtures/rag-postgres-cases.json`. There are 13
fictional queries covering Arabic and English, permitted documents, documents
restricted by role, an inactive document, absent policy, and source page numbers.
The dataset is a small development diagnostic, not an independent held-out
sample or a substitute for labeled real-world evaluation.

Use a **dedicated disposable database** whose name matches
`tawzeef_test_<32 lowercase hexadecimal characters>` and has pgvector installed
in the public schema. Configure `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and
`PGPASSWORD` securely in the process environment, then from `backend` run:

```powershell
$env:TAWZEEF_TEST_POSTGRES = '1'
uv run --locked --extra postgres --extra semantic python -m app.rag_postgres_evaluation
Remove-Item Env:TAWZEEF_TEST_POSTGRES
```

The script verifies the actual connected database name, creates a randomly named
schema, explicitly creates the vector table inside that schema, confirms the
endpoint's connection uses it, and removes only that schema when finished. The
database is not deleted by the script: the operator owns its lifecycle. Do not
run the evaluation against real data or print credentials. The local model files
are checked against the pinned hashes by `LocalE5`. The endpoint runs without
Groq. Use a local PostgreSQL server if the test database must remain on this
machine.

The JSON report records source titles, page numbers, excerpts and similarities,
dataset and runtime hashes, Git revision and dirty status, language-specific
support recall@3, top-1 hits, no-result counts for questions without accessible
support, HTTP errors, role violations, source/page citation violations, and
latency. All returned excerpts in this diagnostic are fictional. It also checks
that unauthorized and inactive source text is not passed to the local embedding
model. A returned candidate is **not** proof of a correct or grounded answer;
the script does not invoke or grade a generative model. No result means only
that this search returned no candidate. Evaluate refusal and source use by the
full answer path separately.

The utility exits nonzero on authorization, citation or HTTP violations; it
reports retrieval quality without a pass threshold because no production
threshold is established. HTTP errors are excluded from retrieval denominators
and reported separately. For meaningful claims, collect independently labeled,
realistic questions and documents for the intended users, including held-out
Arabic and English examples, conflicting policies, section and page citations,
access changes, unanswerable queries and human judgments of generated answers.
