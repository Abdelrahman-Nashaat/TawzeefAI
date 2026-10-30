# Local hybrid-search experiment

This is an opt-in experiment. A separate endpoint and UI button use PostgreSQL
and pgvector when explicitly enabled; the normal answer and chat paths remain
lexical unless separately opted in as described below. The experiment retrieves
candidate excerpts from HR-uploaded knowledge documents,
not verified answers, jobs, or the bundled policy fixtures. Similarity is not
proof of relevance or factual support; the threshold is not production calibrated.

`backend/app/semantic_experiment.py` uses a pinned local multilingual E5 ONNX
model, tokenizes and embeds on CPU, and compares only currently authorized
passages using sqlite-vec in an in-memory database. It combines vector and
existing BM25 ranks and rechecks each source after inference. It does not
send documents or questions over a network or persist vectors. A caller must
derive the role from the authenticated server session, not user input.
The separate optional Groq integration can send selected excerpts externally;
this experiment does not invoke it.

The PostgreSQL path (`app/postgres_retrieval.py`) computes local embeddings,
stores vectors and opaque source identifiers in pgvector, ranks only IDs and
content revisions authorized by the current SQLite state, combines vector and
BM25 ranks, and rechecks active sources before returning their text. It rebuilds
the role-, optional collection- and optional job-specific index on each search and is bounded to 100 passages. Embeddings
can persist in PostgreSQL until the corresponding index is refreshed, even after a document
is disabled; database storage still needs protection. Account revocation is
checked again before the endpoint returns. This is a local demo, not a general
guarantee against every concurrent access or leakage scenario.

## Setup

From `backend`, install the pinned optional dependencies:

```powershell
uv sync --extra semantic
```

Place these two official files under `TawzeefAI/.tooling/multilingual-e5-small/`:

- `model.onnx`: [quantized multilingual-e5-small ONNX](https://huggingface.co/intfloat/multilingual-e5-small/resolve/6a0d452/onnx/model_qint8_avx512_vnni.onnx)
- `tokenizer.json`: [tokenizer](https://huggingface.co/intfloat/multilingual-e5-small/resolve/6a0d452/tokenizer.json)

The component checks both SHA-256 hashes against the pinned upstream files
before loading. The model is an AVX512-VNNI quantized export; it ran using CPU
ONNX Runtime on this development machine, but compatibility and performance
must be tested on each target CPU. Model files are ignored by Git.

To use the PostgreSQL experiment entirely in Docker, run
`start-docker-demo.ps1 -Hybrid` from the project root and open
`http://127.0.0.1:18000/`. Alternatively, start Docker with
`start-docker-demo.ps1`, run `start-hybrid-demo.ps1` and open
`http://127.0.0.1:8000/` for the host version. Have HR add a synthetic policy
document, then use the experimental button on the knowledge panel. The optional
collection selector limits this experimental endpoint to uploaded documents of
the selected type; omitted selection searches all currently authorized uploaded
documents. An optional job filter returns only uploaded documents explicitly
linked to that job; it excludes unlinked documents. Past-hire patterns remain
HR-only regardless of either selector. The
selector does not filter normal answers, saved conversations, published jobs, or
bundled fixtures. These logical vector namespaces are not independent physical
databases and do not supply section metadata filters. The host
and container have separate SQLite databases. The host's regular answer button
can use Groq if the local key is present; the Docker demo does not enable Groq.
Do not use real data in either demo. If the database or model is unavailable,
the experimental endpoint reports an error and normal search remains available.

To additionally test hybrid candidates in the ordinary knowledge answer and
saved-conversation flows, run the host demo with
`$env:TAWZEEF_USE_HYBRID_ANSWERS='1'; .\start-hybrid-demo.ps1` from the project
root. This independent opt-in also requires `TAWZEEF_ENABLE_HYBRID=1`, the
local model, and PostgreSQL configured by that script. Unset the variable after
the test with `Remove-Item Env:TAWZEEF_USE_HYBRID_ANSWERS`. Ordinary answers
then interleave the experimental document candidates with lexical results,
including job and sample sources, and recheck each candidate against current
authorized source content. Session and source permissions are rechecked before
source excerpts are sent to the provider and again before returning or
storing it. A failed or busy hybrid search falls back to lexical search with a
notice. There remains a race after the last check and before a provider call;
these controls do not eliminate all disclosure risk. The hybrid index still
covers uploaded documents only, not all three capstone collections, and it does
not have a separate reranking model. Do not infer answer accuracy from citations
or from similarity scores.

## Evaluation

```powershell
cd backend
uv run --extra semantic python -m app.semantic_evaluation ..\.tooling\multilingual-e5-small
uv run --extra semantic pytest tests/test_semantic_experiment.py -q
```

The SQLite benchmark uses nine synthetic development questions, including three
unanswerable questions, and reports three diagnostic thresholds. It is far too
small to choose a threshold or prove accuracy. Before relying on this integration, use a
separate labeled Arabic/English set, test false returns and missing evidence,
validate page citations, audit authorization and revocation, measure resource
use at realistic corpus sizes, and review the results independently.

On the same nine development questions, the PostgreSQL hybrid path returned
the expected source first for 5/6 answerable questions and within the top three
for 5/6 at similarity 0.85; it returned no source for 3/3 unanswerable examples.
At 0.80 it found 6/6 within the top three but returned a source for 1/3
unanswerable examples. These are diagnostic results from a tiny synthetic set,
not an independent test or an estimate of real-world answer accuracy.
