# Policy retrieval comparison

On 1 November 2026, a local diagnostic compared the existing policy-conversation
retrieval with the separately exposed experimental hybrid search. Both used the
same synthetic handbook and the 30 draft-labelled questions in
`data/capstone_samples/`; the four additional negative cases were checked
separately. The labels have not been independently reviewed. This is a
development diagnostic, not a benchmark of answer accuracy.

The lexical baseline uses the policy-conversation API with Groq and hybrid
retrieval disabled. The hybrid side uses the authenticated, handbook-filtered
experimental endpoint with the local multilingual E5 model and a dedicated
PostgreSQL/pgvector database. Each handbook section was uploaded as a separate
synthetic document, and returned source IDs were compared to the draft expected
section IDs. The hybrid endpoint returns passages, not answers, and is not the
policy conversation agent. Thus these results do not show that the agent can
answer the missed questions or that all retrieved passages support an answer.

| Case group | Lexical expected source in top 3 | Hybrid expected source in top 3 |
| --- | ---: | ---: |
| English | 9/15 | 14/15 |
| Arabic | 6/15 | 14/15 |
| Total | 15/30 | 28/30 |

The hybrid search returned no passages for either `cap-qa-25` or `cap-qa-26`.
All four extra negative cases returned no hybrid passages, and no listed
forbidden section was returned in these cases. This small set does not
establish general authorization safety or a reliable abstention threshold.

Run `backend/evaluate_policy_hybrid.py` only against a dedicated disposable
PostgreSQL database with the pgvector extension already enabled. Set the usual
`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, and `PGDATABASE` variables and set
`TAWZEEF_TEST_POSTGRES=1`. The database name must be `tawzeef_test_` followed
by 32 lowercase hexadecimal characters. The script creates and drops its own
temporary schema, uses temporary SQLite storage, checks input and runtime file
hashes, and outputs per-case source IDs and summary JSON. It expects the local
model files at `.tooling/multilingual-e5-small`; see
`local-semantic-experiment.md`. The recorded run used a temporary loopback-only
`pgvector/pgvector:0.8.7-pg17-bookworm` container which was removed afterward.
Its script exit status was zero. Focused diagnostic tests passed (2 tests).

This result justifies further investigation, not replacing the policy agent's
existing eligibility gate. In particular, it does not validate semantic
support, answer grounding, performance on near-miss or conflicting policies,
source revocation on the hybrid path, robustness across users and datasets,
or a safety threshold. Independently labelled holdout cases and end-to-end
answer evaluation remain necessary.
