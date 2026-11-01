# Experimental policy-answer diagnostic

This is a small development diagnostic, not an independent benchmark. The
questions, one fictional policy, and expected support labels were authored
during development and have not been independently reviewed. They do not
establish answer accuracy, fairness, general authorization safety, or an
acceptable failure rate. The optional hybrid answer path is disabled by
default. Exact source-line verification establishes provenance, not relevance,
completeness, correct interpretation, or entitlement.

On 1 November 2026, the actual conversation endpoint used a temporary SQLite
database, a loopback-only disposable PostgreSQL/pgvector database, the pinned
local multilingual E5 model, and Groq's `openai/gpt-oss-20b` model. The only
policy stated that staff may work remotely two days per week with manager
approval. Both the question and this fictional source were sent to Groq; no
real employee data was used. The temporary container was removed after the run.
Responses are classified below using the draft labels. An abstention includes
any condition that yielded no answer; the diagnostic does not attribute every
miss to a particular stage.

| Case ID | Question | Draft support | Full endpoint | Selector only |
| --- | --- | --- | --- | --- |
| remote | Can employees work from home? | Yes | Exact source line | Exact source line |
| approval | Is manager consent needed to work from home? | Yes | Abstain | Exact source line |
| internet | Does working from home include paid internet? | No | Abstain | Abstain |
| equipment | Does working from home include a company laptop? | No | Abstain | Abstain |
| multipart | Can employees work from home and receive paid internet? | No | Abstain | Abstain |
| salary | How much salary increase is provided for working from home? | No | Abstain | Abstain |
| insurance | Does working from home include additional health insurance? | No | Abstain | Abstain |
| electricity | Does the company reimburse electricity used while working from home? | No | Abstain | Abstain |
| denial | Can managers deny a request to work from home without giving a reason? | No | Abstain | Abstain |
| childcare | Does working from home include a childcare allowance? | No | Abstain | Abstain |

The full endpoint returned the expected source line on 1 of 2 supported
questions and abstained on all 8 unsupported questions. Selector-only testing
supplied the same passage directly and returned it for both supported questions
while abstaining on all 8 unsupported questions. This comparison is too small
to isolate which stage caused the supported miss or to prove robustness. A
separate initial question asking whether employees could work from home every
day received the two-day source line; whether that is a complete answer depends
on interpretation, so it is not counted in the table above.

`backend/evaluate_hybrid_answers_diagnostic.py --selector-only` runs the
selector-only diagnostic with `GROQ_API_KEY` supplied securely in the process
environment. Without that switch, configure a dedicated disposable database
named `tawzeef_test_` plus 32 lowercase hexadecimal characters, PostgreSQL
connection variables, `TAWZEEF_TEST_POSTGRES=1`, and the pinned model directory
in `TAWZEEF_EMBEDDING_MODEL_DIR`; the database requires pgvector. The script
uses a temporary SQLite database and produces case-level JSON. It does not print
the API key or provider error bodies. The output contains the fictional source
line and questions. A provider failure or malformed selection is not a passing
answer; the application abstains for the experimental path. Additional blinded,
independently labelled multilingual cases, contradictions, conflicting source
policies, and end-to-end human answer review are required before relying on it.
