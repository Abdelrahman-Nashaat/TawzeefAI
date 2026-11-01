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

## Follow-up candidate threshold diagnostic

On 1 November 2026, a follow-up run used the same single fictional policy and
the same full conversation endpoint, Groq model, local embedding model and
disposable PostgreSQL/pgvector setup. The earlier consent question had cosine
similarity 0.8285 and was excluded by the 0.85 vector cutoff; the original
remote-work question scored 0.8513. The separately opted-in experimental
answer path now uses 0.82 to select possible passages for model review. This
cutoff was chosen using the consent example and is **not** an independently
validated support threshold. Independent search and the suggestion-only path
retain 0.85. Candidate retrieval still includes lexical matches, so neither
cutoff proves that a selected paragraph answers a question.

The complete conversation returned exact source lines for both original
supported questions and abstained on the original eight unsupported cases.
Eight further draft-labelled questions were added after choosing the cutoff:

| Added case | Draft label | Full endpoint outcome |
| --- | --- | --- |
| `additional-days` | Supported | Source line returned |
| `additional-approval` | Supported | Source line returned |
| `additional-permission` | Supported | Source line returned |
| `additional-ar-consent` | Supported | Abstained |
| `additional-hours` | Unsupported | Abstained |
| `additional-transport` | Unsupported | Abstained after model error or invalid tool response |
| `additional-salary` | Unsupported | Abstained |
| `additional-ar-equipment` | Unsupported | Abstained |

The added cases use the same policy and were authored by the development
team, without independent review; they are not a blinded or independent test
set. The provider-error case is an operational failure, not evidence that the
model correctly recognized unsupported content. The supported Arabic question
remained unanswered. Results can vary between model calls. A real PostgreSQL
test with the local model and a mocked selector separately verified that the
consent passage becomes eligible at the new cutoff. More multilingual policies,
conflicting documents, human-reviewed holdout cases and answer-quality
evaluation remain necessary before relying on this experimental path.

## Translation-assisted retrieval diagnostic

The supported Arabic manager-consent question scored 0.7806 against the English
synthetic policy, below the experimental candidate cutoff of 0.82. A live
translation scored 0.8440. The unsupported Arabic computer question scored
0.7551 before translation and 0.8174 after one translation. These scores are
one run on one policy and do not establish a safe threshold or semantic support.

The separately opted-in `TAWZEEF_POLICY_TRANSLATED_RETRIEVAL=1` path retries an
empty Arabic hybrid retrieval with a model-generated English query. It is only
available when experimental hybrid policy answers and Groq are enabled. The
translation is untrusted and may change meaning. The model still selects exact
lines using the original question; the server validates the lines and rechecks
source access, but cannot verify relevance or completeness. A translation or
provider failure yields abstention. The translated query and question are sent
to the model provider; use synthetic data in the demo.

In one 1 November 2026 live run through the conversation endpoint, a temporary
PostgreSQL/pgvector database, pinned local model, and Groq were used on the same
one-policy development set. The supported English permission question and the
previously missed Arabic consent question returned the source line; unsupported
English salary and Arabic equipment questions abstained. The Arabic consent
response disclosed that retrieval used an unverified translation. Provider
outputs can vary, and these four cases were selected from development data;
they are not independent evidence of answer quality. A separate real-database
test used mocked translations and selections to exercise authorization and the
original-question boundary. No real employee data was used.
