"""Experimental local-only evidence suggestions; never makes hiring decisions."""

import os
import re
from threading import BoundedSemaphore

from fastapi import HTTPException

from app.resume_structure import OTHER_HEADINGS, SECTION_HEADINGS, _label, structure_resume

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
    intervals = []
    for section_name, section in structured['sections'].items():
        for item in section['items']:
            intervals.append((item['start'], item['end'], section_name))
    intervals.sort()
    headings = {heading.casefold() for labels in SECTION_HEADINGS.values() for heading in labels} | OTHER_HEADINGS

    def add(start: int, end: int, section_name: str) -> None:
        content = source[start:end].strip()
        if not content or content == '[identity field removed]':
            return
        if section_name == 'unclassified' and _label(content.rstrip(':：').strip()) in headings:
            return
        while start < end:
            # Prefer sentence/word boundaries. Retain context across long lines
            # so negation near an arbitrary 500-character cut is not lost.
            stop = min(start + 500, end)
            if stop < end:
                fragment = source[start:stop]
                boundaries = list(re.finditer(r'(?<=[.!?؟])\s+', fragment))
                if boundaries and boundaries[-1].end() >= 200:
                    stop = start + boundaries[-1].end()
                else:
                    word_boundary = fragment.rfind(' ')
                    if word_boundary >= 200:
                        stop = start + word_boundary + 1
            excerpt = source[start:stop]
            if excerpt.strip() and excerpt.strip() != '[identity field removed]':
                result.append({'start': start, 'end': stop, 'excerpt': excerpt, 'section': section_name})
                if len(result) > MAX_PASSAGES:
                    raise HTTPException(status_code=422, detail='Too many resume passages for experimental screening')
            if stop == end:
                break
            overlap_start = max(start + 1, stop - 120)
            # Avoid starting in the middle of a word where possible.
            space = source.find(' ', overlap_start, min(stop, overlap_start + 40))
            start = space + 1 if space >= 0 else overlap_start

    # Include remaining lines without claiming a section or a qualification.
    # Preserve offsets into the exact partially redacted source, even if the
    # parser classified only part of a line or truncated an item.
    position = 0
    index = 0
    for line in source.splitlines(keepends=True):
        line_end = position + len(line.rstrip('\r\n'))
        cursor = position
        while index < len(intervals) and intervals[index][0] < line_end:
            start, end, name = intervals[index]
            if cursor < start:
                add(cursor, start, 'unclassified')
            add(start, end, name)
            cursor = max(cursor, end)
            index += 1
        if cursor < line_end:
            add(cursor, line_end, 'unclassified')
        position += len(line)
    return result


def _contextual_passage(source: str, passages: list[dict], index: int) -> dict:
    """Include adjacent lines from the same section when they fit in the span."""
    item = passages[index]
    start, end = item['start'], item['end']
    if index:
        previous = passages[index - 1]
        gap = source[previous['end']:start] if previous['end'] <= start else ''
        if (previous['section'] == item['section'] and re.fullmatch(r'[ \t]*\r?\n[ \t]*', gap)
                and end - previous['start'] <= 500):
            start = previous['start']
    if index + 1 < len(passages):
        following = passages[index + 1]
        gap = source[end:following['start']] if end <= following['start'] else ''
        if (following['section'] == item['section'] and re.fullmatch(r'[ \t]*\r?\n[ \t]*', gap)
                and following['end'] - start <= 500):
            end = following['end']
    return {**item, 'start': start, 'end': end, 'excerpt': source[start:end]}


def _phrase_pattern(label: str) -> re.Pattern | None:
    """Match an entire criterion phrase, never a substring of an identifier."""
    phrase = ' '.join(label.split())
    if not 2 <= len(phrase) <= 200 or not any(char.isalnum() for char in phrase):
        return None
    escaped = re.escape(phrase).replace(r'\ ', r'\s+')
    return re.compile(r'(?<![\w+#])' + escaped + r'(?![\w+#])', re.IGNORECASE)


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
                # Exact phrase occurrences supplement the experimental model;
                # they are mentions for human review, not verified skills.
                pattern = _phrase_pattern(criteria[index]['label'])
                if pattern:
                    exact_lines = set()
                    for require_new_line in (True, False):
                        for item, passage in enumerate(passages):
                            # Search the full source so a passage boundary cannot
                            # fabricate an identifier boundary at either end.
                            match = pattern.search(source, passage['start'])
                            if not match or match.end() > passage['end']:
                                continue
                            line = source.rfind('\n', 0, passage['start']) + 1
                            if require_new_line and line in exact_lines:
                                continue
                            contextual = _contextual_passage(source, passages, item)
                            if any(contextual['start'] < prior['end'] and prior['start'] < contextual['end']
                                   for prior in result[index]['candidates']):
                                continue
                            result[index]['candidates'].append({**contextual, 'similarity': None,
                                                                'retrieval_method': 'exact_phrase',
                                                                'matched_start': match.start(),
                                                                'matched_end': match.end()})
                            exact_lines.add(line)
                            if len(result[index]['candidates']) == 3:
                                break
                        if len(result[index]['candidates']) == 3:
                            break
                ranked = sorted(range(len(passages)), key=lambda i: (-float(row[i]), passages[i]['start']))
                seen_lines = {source.rfind('\n', 0, item['matched_start']) + 1
                              for item in result[index]['candidates']}
                for require_new_line in (True, False):
                    if len(result[index]['candidates']) == 3:
                        break
                    for item in ranked:
                        similarity = float(row[item])
                        if similarity < MIN_SIMILARITY:
                            break
                        line = source.rfind('\n', 0, passages[item]['start']) + 1
                        if require_new_line and line in seen_lines:
                            continue
                        contextual = _contextual_passage(source, passages, item)
                        if any(contextual['start'] < prior['end'] and prior['start'] < contextual['end']
                               for prior in result[index]['candidates']):
                            continue
                        result[index]['candidates'].append({**contextual, 'similarity': round(similarity, 4),
                                                            'retrieval_method': 'semantic',
                                                            'matched_start': passages[item]['start'],
                                                            'matched_end': passages[item]['end']})
                        seen_lines.add(line)
                        if len(result[index]['candidates']) == 3:
                            break
                    if len(result[index]['candidates']) == 3:
                        break
        except Exception as exc:
            # Avoid revealing source text, filesystem paths, or model errors.
            raise HTTPException(status_code=503, detail='Experimental semantic screening is unavailable') from exc
        return result
    finally:
        _busy.release()
