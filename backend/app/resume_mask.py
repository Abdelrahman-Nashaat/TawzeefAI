"""Infrastructure for versioned, human-selected additional résumé masking.

The endpoints and helper are not wired into the application until all screening
consumers use the same source and revision contract. Masking is not anonymization.
"""

import hashlib
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.auth import User, require_roles
from app.blind import redact_resume
from app.db import connect

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]
MAX_SOURCE_CHARS = 200_000
NOTICE = ('Additional masking is selected by HR and may miss identifying information. '
          'Review the source; this is not anonymization or proof of fairness.')


def initialize_resume_masks(path: str | Path) -> None:
    """Install idempotent mask storage and a monotonic raw-source revision."""
    with connect(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS resume_mask_state (
                application_id INTEGER PRIMARY KEY REFERENCES applications(id) ON DELETE CASCADE,
                source_revision INTEGER NOT NULL DEFAULT 0 CHECK(source_revision >= 0),
                mask_revision INTEGER NOT NULL DEFAULT 0 CHECK(mask_revision >= 0)
            );
            CREATE TABLE IF NOT EXISTS resume_masks (
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(revision > 0),
                source_revision INTEGER NOT NULL CHECK(source_revision >= 0),
                source_sha256 TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                spans_json TEXT NOT NULL,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY(application_id, revision)
            );
            CREATE TABLE IF NOT EXISTS resume_mask_prior_scores (
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                mask_revision INTEGER NOT NULL,
                score_json TEXT NOT NULL,
                PRIMARY KEY(application_id, mask_revision)
            );
            CREATE TRIGGER IF NOT EXISTS resume_mask_source_changed
            AFTER UPDATE OF resume_text ON applications WHEN OLD.resume_text IS NOT NEW.resume_text BEGIN
                INSERT OR IGNORE INTO resume_mask_state(application_id) VALUES(NEW.id);
                UPDATE resume_mask_state SET source_revision=source_revision+1 WHERE application_id=NEW.id;
            END;
        ''')


class Span(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)

    @model_validator(mode='after')
    def validate_range(self):
        if self.end <= self.start:
            raise ValueError('Invalid range')
        return self


class MaskSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_source_revision: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=0)
    spans: list[Span] = Field(max_length=200)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _base(db, application_id: int):
    row = db.execute('SELECT resume_text FROM applications WHERE id=?', (application_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Application not found')
    raw = row['resume_text']
    source = redact_resume(raw).text
    if len(source) > MAX_SOURCE_CHARS:
        raise HTTPException(status_code=422, detail='Redacted resume exceeds the review limit')
    state = db.execute('SELECT source_revision,mask_revision FROM resume_mask_state WHERE application_id=?',
                       (application_id,)).fetchone()
    revision = state['mask_revision'] if state else 0
    latest = db.execute('SELECT MAX(revision) FROM resume_masks WHERE application_id=?', (application_id,)).fetchone()[0] or 0
    if revision != latest:
        raise HTTPException(status_code=409, detail='Resume mask state is invalid; review storage before proceeding')
    return raw, source, state['source_revision'] if state else 0, revision


def _apply(source: str, spans: list[Span]) -> str:
    previous_end = 0
    chunks = []
    for span in spans:
        if span.start < previous_end or span.end > len(source) or not source[span.start:span.end].strip():
            raise ValueError('Invalid or overlapping mask spans')
        chunks.append(source[previous_end:span.start])
        chunks.append(''.join(char if char in '\r\n' else ' ' for char in source[span.start:span.end]))
        previous_end = span.end
    chunks.append(source[previous_end:])
    return ''.join(chunks)


def _context_sha256(source_sha256: str, source_revision: int, mask_revision: int) -> str:
    # Keep old unreviewed source fingerprints compatible until a mask or a
    # source revision exists. Never use the text hash alone after either changes.
    if source_revision == 0 and mask_revision == 0:
        return source_sha256
    encoded = json.dumps({'source_sha256': source_sha256, 'source_revision': source_revision,
                          'mask_revision': mask_revision}, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(b'tawzeef-resume-mask-v1\0' + encoded).hexdigest()


def masked_source(db, application_id: int) -> dict:
    """Get the current source or fail closed on stale or invalid saved metadata.

    The caller must hold a read or write transaction and perform authorization.
    ``source_sha256`` always hashes the returned text; ``context_sha256`` also
    binds monotonic revisions and prevents change-and-revert from reviving work.
    """
    raw, base, source_revision, revision = _base(db, application_id)
    status, text = 'unreviewed', base
    if revision:
        row = db.execute('''SELECT source_revision,source_sha256,raw_sha256,spans_json FROM resume_masks
                            WHERE application_id=? AND revision=?''', (application_id, revision)).fetchone()
        if (row is None or row['source_revision'] != source_revision or row['source_sha256'] != _digest(base)
                or row['raw_sha256'] != _digest(raw)):
            raise HTTPException(status_code=409, detail='Resume mask is stale; review it before proceeding')
        try:
            values = json.loads(row['spans_json'])
            if not isinstance(values, list) or len(values) > 200:
                raise ValueError('Invalid stored mask')
            text = _apply(base, [Span.model_validate(value) for value in values])
        except (TypeError, ValueError, KeyError):
            raise HTTPException(status_code=409, detail='Resume mask is invalid; review it before proceeding') from None
        status = 'reviewed'
    source_sha = _digest(text)
    return {'text': text, 'source_sha256': source_sha, 'source_revision': source_revision,
            'mask_revision': revision, 'context_sha256': _context_sha256(source_sha, source_revision, revision),
            'status': status, 'automatic_masked': redact_resume(raw).masked, 'notice': NOTICE}


@router.get('/applications/{application_id}/resume/mask')
def read_mask(application_id: int, request: Request, user: HR):
    # Imported lazily so future screening consumers can import masked_source.
    from app.recruitment import _require_current_role

    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        raw, base, source_revision, revision = _base(db, application_id)
        saved = db.execute('SELECT source_revision,source_sha256,raw_sha256,spans_json FROM resume_masks '
                           'WHERE application_id=? AND revision=?', (application_id, revision)).fetchone() if revision else None
        status, spans = 'unreviewed', []
        if revision:
            try:
                if (saved is None or saved['source_revision'] != source_revision or saved['source_sha256'] != _digest(base)
                        or saved['raw_sha256'] != _digest(raw)):
                    raise ValueError('Stale mask')
                values = json.loads(saved['spans_json'])
                if not isinstance(values, list) or len(values) > 200:
                    raise ValueError('Invalid mask')
                validated = [Span.model_validate(value) for value in values]
                _apply(base, validated)
                status, spans = 'reviewed', [item.model_dump() for item in validated]
            except (TypeError, ValueError, KeyError):
                status = 'stale'
        _require_current_role(db, request, user, 'hr')
        return {'source': base, 'source_sha256': _digest(base), 'source_revision': source_revision,
                'revision': revision, 'status': status, 'spans': spans,
                'offset_unit': 'unicode_code_points', 'notice': NOTICE}


@router.put('/applications/{application_id}/resume/mask')
def save_mask(application_id: int, payload: MaskSave, request: Request, user: HR):
    from app.recruitment import _require_current_role

    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        raw, base, source_revision, revision = _base(db, application_id)
        if (payload.source_sha256 != _digest(base) or payload.expected_source_revision != source_revision
                or payload.expected_revision != revision):
            raise HTTPException(status_code=409, detail='Resume or mask changed; reload before saving')
        try:
            _apply(base, payload.spans)
        except ValueError:
            raise HTTPException(status_code=422, detail='Invalid selection in redacted resume') from None
        db.execute('INSERT OR IGNORE INTO resume_mask_state(application_id) VALUES(?)', (application_id,))
        db.execute('UPDATE resume_mask_state SET mask_revision=mask_revision+1 WHERE application_id=?', (application_id,))
        db.execute('''INSERT INTO resume_masks(application_id,revision,source_revision,source_sha256,raw_sha256,spans_json,created_by)
                      VALUES(?,?,?,?,?,?,?)''', (application_id, revision + 1, source_revision, _digest(base), _digest(raw),
                      json.dumps([span.model_dump() for span in payload.spans]), user.id))
        # A saved score belongs to the old source. Preserve it separately as a
        # historical record and prevent later workflows treating it as current.
        db.execute('''INSERT INTO resume_mask_prior_scores(application_id,mask_revision,score_json)
                      SELECT id,?,score_json FROM applications WHERE id=? AND score_json IS NOT NULL''',
                   (revision + 1, application_id))
        db.execute('UPDATE applications SET score_json=NULL WHERE id=? AND score_json IS NOT NULL', (application_id,))
        db.execute('INSERT OR IGNORE INTO rubric_publication_revisions(application_id,revision) VALUES(?,0)', (application_id,))
        db.execute('UPDATE rubric_publication_revisions SET revision=revision+1 WHERE application_id=?', (application_id,))
        db.execute('DELETE FROM rubric_publications WHERE application_id=?', (application_id,))
        db.execute('INSERT INTO audit_events(application_id,actor_id,action,details) VALUES(?,?,?,?)',
                   (application_id, user.id, 'resume_mask_reviewed',
                    json.dumps({'revision': revision + 1, 'span_count': len(payload.spans)})))
        _require_current_role(db, request, user, 'hr')
        result = masked_source(db, application_id)
        return {key: value for key, value in result.items() if key != 'text'}


@router.get('/applications/{application_id}/resume/mask/prior-scores')
def prior_scores(application_id: int, request: Request, user: HR):
    """Return old scores archived when a mask review invalidated them."""
    from app.recruitment import _require_current_role

    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        _base(db, application_id)
        rows = db.execute('''SELECT p.mask_revision,p.score_json,m.created_at FROM resume_mask_prior_scores p
                             JOIN resume_masks m ON m.application_id=p.application_id AND m.revision=p.mask_revision
                             WHERE p.application_id=? ORDER BY p.mask_revision DESC LIMIT 100''',
                          (application_id,)).fetchall()
        entries = []
        for row in rows:
            try:
                result = json.loads(row['score_json'])
                if not isinstance(result, dict):
                    raise TypeError('Invalid archived score')
            except (TypeError, ValueError):
                result = None
            entries.append({'mask_revision': row['mask_revision'], 'archived_at': row['created_at'], 'result': result})
        _require_current_role(db, request, user, 'hr')
        return {'entries': entries, 'notice': 'Historical scores predate the selected mask; they are not current.'}
