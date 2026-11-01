# Policy conversation: broader synthetic diagnostic

On 1 November 2026, the policy conversation API was exercised on the bundled
synthetic handbook: 30 draft-labelled answerable questions (15 English, 15
Arabic) and four additional negative/access cases. The handbook is fictional,
and its reference answers have not been independently reviewed. One logical
handbook section was uploaded per text document; this is not a PDF pagination
test. The experimental hybrid answer and translated-retrieval options were on.
The run used the pinned local E5 model, an isolated PostgreSQL/pgvector
container, and Groq; only synthetic questions and authorized synthetic source
passages were sent. The temporary container was removed afterward.

The complete case-level artifact, including questions, draft expected answers,
returned answers and source identifiers, is at
`data/evaluation_runs/policy-answers-full-2026-11-01.json`. It also records
dataset and runtime file hashes. Every `human_correctness` field is null: an
independent human must evaluate the content, context and completeness of each
answer before answer accuracy can be reported. A matching source ID is a
retrieval measure and does not establish that the answer is correct.

- A draft-expected source appeared among returned sources for 24/30 answerable
  cases: 14/15 English and 10/15 Arabic. This includes extractive fallbacks.
- The output modes across all 34 cases were 15 `groq`, 9 `extractive`, and 10
  `abstain`.
- Instrumentation observed 20 call exceptions across 13 cases: 11 while
  requesting a search decision and 9 while requesting quote selection. The
  diagnostic records stage and error occurrence only, not exception bodies,
  credentials, or a definitive cause. Model validation failures can produce
  notices without an exception from the instrumented calls; 19 cases carried
  an unavailable, invalid, or unverifiable notice. These are not successful
  model decisions merely because a fallback returned a source.
- All four negative cases abstained and no listed forbidden source appeared.
  One of those cases had an observed call exception, so only three were
  abstentions without an observed call exception. This is not proof of access
  isolation in general, nor evidence that an outage is a correct abstention.

In the table, source means at least one draft-expected source appeared in the
first three returned source IDs. Error means an exception was observed in one
or more instrumented calls; invalid model output can also be reported in the
notice without such an exception. Negative cases have no expected source.

| Case | Mode | Draft-expected source | Observed call error |
| --- | --- | --- | --- |
| cap-qa-01 | groq | yes | no |
| cap-qa-02 | extractive | yes | no |
| cap-qa-03 | extractive | yes | no |
| cap-qa-04 | groq | yes | no |
| cap-qa-05 | groq | yes | no |
| cap-qa-06 | groq | yes | yes |
| cap-qa-07 | groq | yes | no |
| cap-qa-08 | groq | yes | no |
| cap-qa-09 | extractive | yes | no |
| cap-qa-10 | groq | yes | no |
| cap-qa-11 | groq | yes | no |
| cap-qa-12 | groq | yes | no |
| cap-qa-13 | groq | yes | no |
| cap-qa-14 | groq | yes | no |
| cap-qa-15 | groq | yes | no |
| cap-qa-16 | abstain | no | yes |
| cap-qa-17 | extractive | yes | yes |
| cap-qa-18 | abstain | no | yes |
| cap-qa-19 | groq | yes | no |
| cap-qa-20 | abstain | no | yes |
| cap-qa-21 | groq | yes | yes |
| cap-qa-22 | abstain | no | yes |
| cap-qa-23 | extractive | yes | no |
| cap-qa-24 | extractive | yes | yes |
| cap-qa-25 | abstain | no | yes |
| cap-qa-26 | groq | yes | no |
| cap-qa-27 | extractive | yes | yes |
| cap-qa-28 | abstain | no | yes |
| cap-qa-29 | extractive | yes | no |
| cap-qa-30 | extractive | yes | yes |
| negative-access-en | abstain | n/a | no |
| negative-access-ar | abstain | n/a | yes |
| negative-unsupported | abstain | n/a | no |
| negative-injection | abstain | n/a | no |

To repeat, configure a dedicated disposable PostgreSQL database named
`tawzeef_test_` followed by 32 lowercase hexadecimal characters, enable
pgvector, set `TAWZEEF_TEST_POSTGRES=1` and the usual PostgreSQL variables,
and supply the provider key securely in the process environment. From
`backend`, run `python evaluate_policy_answers_full.py` with the pinned local
model in `.tooling/multilingual-e5-small`. The program refuses other database
names, uses a temporary schema and SQLite database, and verifies input runtime
hashes. It outputs JSON containing synthetic questions and answers. Its event
instrumentation changes no answer behavior but cannot identify all malformed
model responses, provider causes, or answer correctness. Rate limits, model
updates, the bilingual document structure, and small draft-labelled sample
limit generalization; independent adjudication and held-out cases are required.
