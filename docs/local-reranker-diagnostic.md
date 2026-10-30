# Local reranker diagnostic

On 30 October 2026 a local CPU-only experiment compared the current SQLite
experimental E5 hybrid retrieval with a separate cross-encoder reranker on its
existing nine fictional development questions. These are development cases,
not a held-out benchmark or a test of PostgreSQL, generated answers, access
control, or production quality. Reordering the top three candidates cannot
recover a relevant passage that retrieval did not return and does not establish
that a candidate answers the question.

The reranker files were downloaded from the [model author's repository](https://huggingface.co/Horizon-Labs/multilingual-reranker-small)
at revision `ab6e3ce89956cd33d919807137848b90a5cce77d`, which declares the
Apache-2.0 license, into the ignored local `.tooling/reranker-test/` directory.
Weights and tokenizer are not committed to Git. The local diagnostic verified
these hashes:

| Artifact | SHA-256 |
| --- | --- |
| `model.onnx` | `f913154682468bfe92d6a7e02ed4e994e548c112fddc74452b573b17c949046e` |
| `tokenizer.json` | `7925f638b5444897efe3b4608eed56c1652f0ed39c2662c8302de1bc264c6088` |

The local script `.tooling/reranker-test/benchmark.py` used the pinned E5
model and a temporary SQLite database populated only with fictional documents.
It re-ranked at most three retrieved passages without a separate reranker score
threshold. Figures below count six questions with a supporting document and
three without one; the same cases were used in earlier development, so threshold
selection or claims of generalization would be inappropriate.

| E5 similarity cutoff | Baseline correct top 1 | Reranked correct top 1 | Recall at 3, both | Unsupported questions returning candidates |
| --- | --- | --- | --- | --- |
| 0.75 | 5/6 | 6/6 | 6/6 | 3/3 |
| 0.80 | 5/6 | 6/6 | 6/6 | 1/3 |
| 0.85 | 3/6 | 3/6 | 3/6 | 0/3 |

The single improved ranking at 0.75 and 0.80 was an Arabic question about a
probation policy. At 0.85 the underlying retrieval missed that passage, so the
reranker could not help. The experiment measured approximately 21.4 seconds
to initialize the reranker, a combined peak process working set of 880 MiB,
and mean re-ranking time of 0.23–0.62 seconds per nonempty question across
cutoffs; these are one machine and one run, not a capacity benchmark.

The reranker is **not integrated** into the application. Before considering
integration, use an independently labelled bilingual held-out set and compare
the actual PostgreSQL path with and without reranking, including authorization,
changed/revoked sources, no-support queries, latency and memory. A safe
abstention policy also requires separate evaluation; the reranker in this
experiment only reorders existing candidates.
