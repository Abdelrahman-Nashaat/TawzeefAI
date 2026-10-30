# Synthetic résumé retrieval: diagnostic run

This is a **development diagnostic**, not a hiring or fairness validation.
All 14 cases are fictional. The actual local HR endpoint processed uploaded
DOCX files in an isolated temporary SQLite database using the pinned local
embedding model. The run took place on 2026-10-30 at 01:37 UTC; the checked-out
Git HEAD was `fb51bac7b4f2eca110918f986860490b71b1263f` with uncommitted changes.
Windows 10, Python 3.13.2; cosine cutoff 0.85, up to three excerpts. This
cutoff is not calibrated. Dataset SHA-256:
`42532c0302fdac6db7f1a53d151c97e2f78183f51999fa3941373039eecdda5b`.
The local model loader verified model and tokenizer hashes
`dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88` and
`0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39`.

Support recall@3 was **6/7**: English 3/4, Arabic 2/2, cross-language 1/1.
One of two no-support cases returned no candidates; the other returned an
unrelated excerpt. Candidate and employee requests to the HR endpoint returned
HTTP 403. The two synthetic name/gender variants returned identical candidates.
These are counts for this small set only.

| Case | Reference label | Outcome |
| --- | --- | --- |
| en-synonym-analysis | support | Supporting excerpt returned. |
| en-experience | support | No excerpt returned; supporting evidence missed. |
| en-negation | negated | No excerpt returned. |
| en-aspiration | unconfirmed | No excerpt returned. |
| en-similar-role | no support | No excerpt returned. |
| en-ambiguous-context | unconfirmed | No excerpt returned. |
| ar-analysis | support | Supporting excerpt returned. |
| ar-api | support | Supporting excerpt and an unrelated excerpt returned. |
| ar-negation | negated | No excerpt returned. |
| ar-aspiration | unconfirmed | The aspiration and an unrelated excerpt returned. |
| ar-no-support | no support | An unrelated excerpt returned. |
| en-counterfactual-a | support | Supporting excerpt returned. |
| en-counterfactual-b | support | Same excerpt and similarity as the paired case. |
| cross-language | support | Supporting excerpt returned. |

Returned negations, aspirations and unrelated excerpts must never be treated as
proof that a criterion is met. A missing result does not prove the person lacks a
skill. Review and larger held-out, human-labeled data for an actual role remain
necessary; the two no-support examples and one identity pair do not establish
precision or fairness.

To identify the uncommitted runtime at this run, SHA-256 hashes of key files were:

| File under `backend/app` | SHA-256 |
| --- | --- |
| `semantic_screening.py` | `7f8b5287a88ee6d87e0bbc788712d9b5539c7ab6029f9020e2a79c20e22e10f8` |
| `rubric_admin.py` | `2cd4fb23d8768ae2d2b3e06183f1eee3706988c5f89f02ce6a7a875d4eee5888` |
| `resume_structure.py` | `33e8a003085304ced98332c26c015a576c68cf07ea485aa9f020708c8b7eb16d` |
| `blind.py` | `4a2b18caf4629da7a7a3337c7bf2fa0044cc8284b6678ae57310b7c23ca86389` |
| `semantic_experiment.py` | `d796fbb66b5b2e66e83bb74f19dda5b7d1f5f9c50392435c430be9e4c8d5caf1` |
| `hybrid_api.py` | `69539160ae9a3a2c1bf80cc9f1071c6a5ec7dff2d99e97a96c7cbc3dbed0d067` |
| `evaluate_semantic_screening.py` | `5a78adc5e443ad810e3b1a89ceeff06efdd981628a1133c569860c143ee2801d` |
