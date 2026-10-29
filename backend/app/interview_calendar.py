"""Export an existing local interview as an iCalendar file; no external sync."""

import hashlib
import unicodedata
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import User, get_current_user
from app.db import connect

router = APIRouter(prefix='/api')
Authenticated = Annotated[User, Depends(get_current_user)]


def _text(value: str) -> str:
    """Encode RFC 5545 TEXT values without allowing property injection."""
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    value = ''.join(c for c in value if c == '\n' or not unicodedata.category(c).startswith('C'))
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def _fold(line: str) -> str:
    """Fold UTF-8 text at 75 octets without splitting a code point."""
    lines, current, length = [], '', 0
    for character in line:
        width = len(character.encode('utf-8'))
        if length + width > 75:
            lines.append(current)
            current, length = ' ', 1
        current += character
        length += width
    lines.append(current)
    return '\r\n'.join(lines)


def _instant(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None or timestamp.microsecond:
            raise ValueError('Unrepresentable timestamp')
        return timestamp.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
    except (ValueError, AttributeError, OverflowError) as exc:
        raise HTTPException(status_code=409, detail='Interview time cannot be represented accurately in iCalendar') from exc


@router.get('/interviews/{interview_id}/calendar')
def interview_calendar(interview_id: int, request: Request, user: Authenticated):
    if user.role not in ('candidate', 'hr'):
        raise HTTPException(status_code=403, detail='Access denied')
    with connect(request.app.state.database_path) as db:
        row = db.execute('''SELECT i.id,i.application_id,i.candidate_id,i.start_at,i.end_at,
            i.status,i.created_at,a.candidate_id AS application_owner,j.title
            FROM interviews i JOIN applications a ON a.id=i.application_id
            JOIN jobs j ON j.id=a.job_id WHERE i.id=?''', (interview_id,)).fetchone()
        if row is None or (user.role == 'candidate' and row['candidate_id'] != user.id):
            raise HTTPException(status_code=404, detail='Interview not found')
        if row['candidate_id'] != row['application_owner']:
            raise HTTPException(status_code=409, detail='Interview ownership is inconsistent')
        if row['status'] != 'scheduled':
            raise HTTPException(status_code=409, detail='Cancelled interviews cannot be exported as active events')
        start = _instant(row['start_at'])
        end = _instant(row['end_at'])
        if end <= start:
            raise HTTPException(status_code=409, detail='Interview interval is invalid')
        # DTSTAMP needs seconds. SQLite creation timestamps include fractions;
        # truncation here affects only metadata, never the scheduled interval.
        try:
            created = datetime.fromisoformat(row['created_at'])
            if created.tzinfo is None or created.utcoffset() is None:
                raise ValueError('Missing timezone')
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=409, detail='Interview creation time is invalid') from exc
        stamp = created.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
        uid = hashlib.sha256(f"{row['id']}:{row['created_at']}".encode()).hexdigest()
        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//TawzeefAI//Local interviews//EN',
                 'CALSCALE:GREGORIAN', 'BEGIN:VEVENT', f'UID:{uid}@tawzeefai.local', f'DTSTAMP:{stamp}',
                 f'DTSTART:{start}', f'DTEND:{end}', f"SUMMARY:{_text('Interview: ' + row['title'])}",
                 'END:VEVENT', 'END:VCALENDAR']
        body = ('\r\n'.join(_fold(line) for line in lines) + '\r\n').encode('utf-8')
    return Response(body, media_type='text/calendar', headers={
        'Content-Disposition': f'attachment; filename="interview-{interview_id}.ics"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    })
