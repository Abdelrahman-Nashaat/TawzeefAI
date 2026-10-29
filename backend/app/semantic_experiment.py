"""Offline hybrid retrieval experiment. Not PostgreSQL or production RAG.

Only the server-authorized role may be passed here. No raw query, document,
or vector is written to disk or sent over the network by this module. The
embedding model must be supplied locally and the caller must not wrap it in a
remote service. Results are evidence excerpts, not verified answers.
"""

import hashlib
import sqlite3
from pathlib import Path
from threading import Lock

import numpy as np
import onnxruntime as ort
import sqlite_vec
from tokenizers import Tokenizer

from app.knowledge import _document_passages, _rank_lexical, _tokens
from app.knowledge_documents import fetch_authorized_documents

MODEL_SHA256 = 'dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88'
TOKENIZER_SHA256 = '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'
MAX_PASSAGES = 100


def _check_hash(path: Path, expected: str):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError(f'Unexpected hash for {path.name}')


class LocalE5:
    """Pinned ONNX and tokenizer files only; CPU inference without remote code."""

    def __init__(self, model_dir: str | Path):
        root = Path(model_dir)
        model = root / 'model.onnx'
        tokenizer = root / 'tokenizer.json'
        _check_hash(model, MODEL_SHA256)
        _check_hash(tokenizer, TOKENIZER_SHA256)
        self.tokenizer = Tokenizer.from_file(str(tokenizer))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model), sess_options=options, providers=['CPUExecutionProvider'])
        self.lock = Lock()

    def encode(self, texts: list[str], *, query: bool) -> np.ndarray:
        if not texts:
            return np.empty((0, 384), dtype=np.float32)
        vectors = []
        with self.lock:
            for start in range(0, len(texts), 4):
                batch = texts[start:start + 4]
                encoded = self.tokenizer.encode_batch([('query: ' if query else 'passage: ') + text for text in batch])
                if any(item.overflowing for item in encoded):
                    raise ValueError('A query or passage exceeds the 512-token model limit')
                ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
                mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
                inputs = {'input_ids': ids, 'attention_mask': mask}
                if any(item.name == 'token_type_ids' for item in self.session.get_inputs()):
                    inputs['token_type_ids'] = np.asarray([item.type_ids for item in encoded], dtype=np.int64)
                hidden = self.session.run(None, inputs)[0]
                pooled = (hidden * mask[:, :, None]).sum(axis=1) / np.maximum(mask.sum(axis=1, keepdims=True), 1)
                norms = np.linalg.norm(pooled, axis=1, keepdims=True)
                if np.any(norms == 0) or not np.all(np.isfinite(pooled)):
                    raise ValueError('Invalid embedding from model')
                vectors.append((pooled / norms).astype(np.float32))
        return np.concatenate(vectors)


def search_hybrid(path: str | Path, role: str, question: str, embedder, *,
                  min_similarity: float = 0.85, limit: int = 3) -> dict:
    """Search only currently authorized DB documents, rechecking before output.

    `min_similarity` is experimental, not a calibrated factuality threshold.
    No documents means abstention. This experiment does not index sample files.
    """
    if not isinstance(question, str) or len(question) > 2000:
        raise ValueError('Question must be a string of at most 2000 characters')
    if not 0 <= min_similarity <= 1 or not 1 <= limit <= 10:
        raise ValueError('Invalid search settings')
    if not question.strip():
        return {'status': 'abstain', 'sources': []}
    documents = fetch_authorized_documents(path, role)
    source_pairs = [(doc, source) for doc in documents for source in _document_passages(doc)]
    if not source_pairs:
        return {'status': 'abstain', 'sources': []}
    if len(source_pairs) > MAX_PASSAGES:
        raise ValueError('Too many passages for the local experiment')
    sources = [pair[1] for pair in source_pairs]
    vectors = np.asarray(embedder.encode([source.text for source in sources], query=False), dtype=np.float32)
    query = np.asarray(embedder.encode([question], query=True), dtype=np.float32)
    if (vectors.ndim != 2 or len(vectors) != len(sources) or query.shape != (1, vectors.shape[1])
            or not np.isfinite(vectors).all() or not np.isfinite(query).all()
            or np.any(np.linalg.norm(vectors, axis=1) == 0) or np.linalg.norm(query) == 0):
        raise ValueError('Invalid embeddings')

    # The exact scan runs only over pre-authorized passages in RAM.
    with sqlite3.connect(':memory:') as db:
        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)
        db.execute('CREATE TABLE embeddings (id INTEGER PRIMARY KEY, vector BLOB NOT NULL)')
        db.executemany('INSERT INTO embeddings (id, vector) VALUES (?, ?)',
                       [(index, vector.tobytes()) for index, vector in enumerate(vectors)])
        ranked = db.execute('''SELECT id, vec_distance_cosine(vector, ?) AS distance
            FROM embeddings ORDER BY distance, id''', (query[0].tobytes(),)).fetchall()
    lexical = _rank_lexical(_tokens(question), sources)
    lexical_ranks = {source.id: rank for rank, (_, source) in enumerate(lexical, 1)}
    scores = []
    for rank, (index, distance) in enumerate(ranked, 1):
        similarity = 1 - distance
        if similarity < min_similarity:
            continue
        source = sources[index]
        fusion = 1 / (60 + rank)
        if source.id in lexical_ranks:
            fusion += 1 / (60 + lexical_ranks[source.id])
        scores.append((fusion, similarity, index))
    scores.sort(key=lambda row: (-row[0], -row[1], row[2]))

    # Check active/audience and exact contents again after slow inference. No
    # stale or changed passage is returned if authorization changed meanwhile.
    live = {doc['id']: doc for doc in fetch_authorized_documents(path, role)}
    result = []
    for _, similarity, index in scores:
        doc, source = source_pairs[index]
        current = live.get(doc['id'])
        if current is None or current != doc:
            continue
        result.append({'document_id': doc['id'], 'id': source.id, 'title': source.title,
                       'page': source.page, 'text': source.text, 'similarity': similarity})
        if len(result) == limit:
            break
    return {'status': 'ok' if result else 'abstain', 'sources': result,
            'notice': 'Experimental candidates only; similarity does not establish support or correctness.'}
