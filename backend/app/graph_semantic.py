"""Source-bound references for local semantic review checkpoints."""

import hashlib
import json
import math

from app.blind import redact_resume
from app.recruitment import get_application
from app.rubric_admin import public_criterion
from app.semantic_screening import _phrase_pattern


def _fingerprint(criteria):
    data = json.dumps(criteria, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


def context(db, application_id):
    application = get_application(db, application_id)
    revision = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                          (application_id,)).fetchone()
    return {'candidate_id': application['candidate_id'], 'job_id': application['job_id'],
            'revision': revision['revision'] if revision else 0}


def save_references(result, original_context):
    """Retain only criterion IDs and offsets, never résumé text or labels."""
    criteria = [{key: item[key] for key in ('criterion_ref', 'job_id', 'category', 'label')}
                for item in result['criteria']]
    references = []
    for item in result['criteria']:
        candidates = []
        for candidate in item['candidates']:
            saved = {key: candidate[key] for key in
                     ('start', 'end', 'matched_start', 'matched_end', 'similarity', 'section')}
            saved['retrieval_method'] = candidate.get('retrieval_method', 'semantic')
            candidates.append(saved)
        references.append({'criterion_ref': item['criterion_ref'], 'candidates': candidates})
    return {'source_sha256': result['source_sha256'], 'criteria_sha256': _fingerprint(criteria),
            'context': original_context,
            'refs': references}


def restore_references(db, application_id, saved):
    """Rebuild source excerpts only for unchanged source and rubric inputs."""
    unavailable = {'status': 'stale', 'notice': 'Resume or criteria changed; start a new semantic review.'}
    try:
        application = get_application(db, application_id)
        source = redact_resume(application['resume_text']).text
        criteria = [public_criterion(row) for row in db.execute(
            'SELECT * FROM rubric_criteria WHERE job_id=? ORDER BY id', (application['job_id'],))]
        if (saved['context'] != context(db, application_id)
                or saved['source_sha256'] != hashlib.sha256(source.encode()).hexdigest()
                or saved['criteria_sha256'] != _fingerprint(criteria)):
            return unavailable
        refs = saved['refs']
        if len(refs) != len(criteria):
            return unavailable
        restored = []
        for criterion, entry in zip(criteria, refs, strict=True):
            if entry['criterion_ref'] != criterion['criterion_ref'] or len(entry['candidates']) > 3:
                return unavailable
            candidates = []
            for item in entry['candidates']:
                start, end = item['start'], item['end']
                matched_start, matched_end = item['matched_start'], item['matched_end']
                score = item['similarity']
                method = item.get('retrieval_method', 'semantic')
                if (any(type(value) is not int for value in (start, end, matched_start, matched_end))
                        or not (0 <= start <= matched_start < matched_end <= end <= len(source))
                        or end - start > 500 or method not in ('exact_phrase', 'semantic')
                        or item['section'] not in ('skills', 'experience', 'education', 'projects', 'languages', 'unclassified')):
                    return unavailable
                if method == 'semantic':
                    if type(score) not in (float, int) or not math.isfinite(score) or not -1 <= score <= 1:
                        return unavailable
                else:
                    pattern = _phrase_pattern(criterion['label'])
                    match = pattern.match(source, matched_start) if pattern else None
                    if score is not None or match is None or match.end() != matched_end:
                        return unavailable
                candidates.append({**item, 'retrieval_method': method, 'excerpt': source[start:end]})
            restored.append({**criterion, 'candidates': candidates})
        return {'status': 'current', 'source_sha256': saved['source_sha256'],
                'offset_unit': 'unicode_code_points', 'criteria': restored,
                'notice': 'Experimental suggestions only; review the full source. Similarity does not establish a qualification.'}
    except (KeyError, TypeError, ValueError, OverflowError):
        return unavailable
