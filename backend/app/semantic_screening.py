"""Experimental local-only evidence suggestions; never makes hiring decisions."""

import os
import re
from threading import BoundedSemaphore

from fastapi import HTTPException

from app.resume_structure import structure_resume

_busy = BoundedSemaphore(1)
MAX_PASSAGES = 100
MIN_SIMILARITY = 0.85  # Uncalibrated diagnostic cutoff, not proof of relevance.


def _model(directory: str):
    # Reuse the pinned local model and its internal inference lock.
    from app.hybrid_api import _load_model

    return _load_model(directory)


def _passages(source: str) -> list[dict]:
    structured = structure_resume(source)
    result = []
    for section_name, section in structured['sections'].items():
        for item in section['items']:
            # Prefer sentence/word boundaries. Retain context across long lines
            # so negation near an arbitrary 500-character cut is not lost.
            start = item['start']
            while start < item['end']:
                end = min(start + 500, item['end'])
                if end < item['end']:
                    fragment = source[start:end]
                    boundaries = list(re.finditer(r'(?<=[.!?؟])\s+', fragment))
                    if boundaries and boundaries[-1].end() >= 200:
                        end = start + boundaries[-1].end()
                    else:
                        word_boundary = fragment.rfind(' ')
                        if word_boundary >= 200:
                            end = start + word_boundary + 1
                excerpt = source[start:end]
                if excerpt.strip():
                    result.append({'start': start, 'end': end, 'excerpt': excerpt, 'section': section_name})
                if end == item['end']:
                    break
                overlap_start = max(start + 1, end - 120)
                # Avoid starting in the middle of a word where possible.
                space = source.find(' ', overlap_start, min(end, overlap_start + 40))
                start = space + 1 if space >= 0 else overlap_start
    if len(result) > MAX_PASSAGES:
        raise HTTPException(status_code=422, detail='Too many resume passages for experimental screening')
    return result


def suggest(source: str, criteria: list[dict]) -> list[dict]:
    """Return up to three candidate source spans for each criterion.

    This never writes assessments, evidence, embeddings or source to storage.
    Source redaction is partial and ranking cannot verify a qualification.
    """
    directory = os.environ.get('TAWZEEF_EMBEDDING_MODEL_DIR', '')
    if os.environ.get('TAWZEEF_ENABLE_SEMANTIC_SCREENING') != '1' or not directory:
        raise HTTPException(status_code=503, detail='Experimental semantic screening is not configured')
    if not _busy.acquire(blocking=False):
        raise HTTPException(status_code=429, detail='Experimental semantic screening is busy')
    try:
        passages = _passages(source)
        result = [{**criterion, 'candidates': []} for criterion in criteria]
        if not passages or not criteria:
            return result
        try:
            import numpy as np

            model = _model(directory)
            vectors = np.asarray(model.encode([p['excerpt'] for p in passages], query=False), dtype=np.float32)
            queries = np.asarray(model.encode([c['label'] for c in criteria], query=True), dtype=np.float32)
            if (vectors.ndim != 2 or queries.ndim != 2 or vectors.shape[0] != len(passages)
                    or queries.shape[0] != len(criteria) or vectors.shape[1] != queries.shape[1]
                    or vectors.shape[1] == 0 or not np.isfinite(vectors).all() or not np.isfinite(queries).all()):
                raise ValueError('Invalid model output')
            passage_norms = np.linalg.norm(vectors, axis=1)
            query_norms = np.linalg.norm(queries, axis=1)
            if np.any(passage_norms == 0) or np.any(query_norms == 0):
                raise ValueError('Invalid model output')
            similarities = (queries @ vectors.T) / (query_norms[:, None] * passage_norms[None, :])
            for index, row in enumerate(similarities):
                ranked = sorted(range(len(passages)), key=lambda i: (-float(row[i]), passages[i]['start']))
                for item in ranked[:3]:
                    similarity = float(row[item])
                    if similarity >= MIN_SIMILARITY:
                        result[index]['candidates'].append({**passages[item], 'similarity': round(similarity, 4)})
        except Exception as exc:
            # Avoid revealing source text, filesystem paths, or model errors.
            raise HTTPException(status_code=503, detail='Experimental semantic screening is unavailable') from exc
        return result
    finally:
        _busy.release()
