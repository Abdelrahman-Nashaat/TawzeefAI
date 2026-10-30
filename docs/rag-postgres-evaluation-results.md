# PostgreSQL hybrid retrieval: diagnostic result

This is a small fictional **development diagnostic**, not a claim of production
accuracy, safety, fairness, or answer correctness. On 2026-10-30 at 01:48 UTC,
the authenticated endpoint ran the pinned local model and PostgreSQL/pgvector
against 13 questions in a dedicated disposable database. The checked-out HEAD
was `3f17c1bc6ac5249f4cc74a58912d64fe423e3bbe`, with uncommitted changes;
the runtime hashes below identify the actual evaluated files. The synthetic
dataset SHA-256 was
`bda7a673c5ec6efb0f88cdaee4cde517be63f38e7fbc151642c76524fb6233d5`.

The expected document and page appeared within the first three candidates in
**5/7** answerable cases: English **4/5**, Arabic **1/2**. All five were ranked
first. The endpoint returned no candidates for **5/6** questions with no
accessible supporting document. One unsupported parental-leave question returned
the unrelated annual-leave policy. There were no HTTP, authorization, or
source/page violations in these cases. This narrow result does not establish
that access control or citations are correct generally.

| Case | Outcome |
| --- | --- |
| en-leave, ar-leave | Expected annual-leave page returned. |
| en-remote | Expected remote-policy page returned. |
| en-interview | No candidate; expected interview page missed. |
| ar-probation | No candidate; expected probation page missed. |
| staff-expense | Expected staff expense page returned for employee. |
| hr-allocation | Expected allocation page returned for HR. |
| candidate-staff-restricted, employee-hr-restricted, candidate-hr-restricted | No candidates; restricted text was absent from the model's passage inputs. |
| en-no-parental-policy | Unrelated annual-leave page returned. |
| ar-no-annual-bonus, en-no-health-premium | No candidates. |

No generated answer was assessed. In particular, returning the annual-leave
document for parental leave shows why retrieval alone cannot guarantee a correct
answer or an appropriate abstention. This dataset has only two relevant Arabic
questions, a few authorization cases, and no human-labeled real policies.

| Evaluated runtime file under `backend/app` | SHA-256 |
| --- | --- |
| `auth.py` | `b366243080baa579fc0e51685936db7fd4874f194184fc86662055b5426a7a34` |
| `hybrid_api.py` | `69539160ae9a3a2c1bf80cc9f1071c6a5ec7dff2d99e97a96c7cbc3dbed0d067` |
| `knowledge.py` | `307f74a4b1d33c4698465e59ff0e05d19c6dc963dd43c02ebf05281384a4536b` |
| `knowledge_documents.py` | `b62d0921ec7118411dfa0163e0b226e6ea0c9495963cd1a298bcec0ea7326653` |
| `postgres_retrieval.py` | `41713332c7710f9b686640a78756bc56396901399559ace02e13a478d39a550e` |
| `postgres_vectors.py` | `41d0b73a5a446bbd0d0cd423ee0333734decf0247610f2d1e35024e935ec7e25` |
| `semantic_experiment.py` | `d796fbb66b5b2e66e83bb74f19dda5b7d1f5f9c50392435c430be9e4c8d5caf1` |

The model and tokenizer were verified against their pinned SHA-256 values,
`dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88` and
`0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39`.
See [procedure and limitations](rag-postgres-evaluation.md).
