"""Shared local review templates; no Google credentials or network dependencies."""

from datetime import UTC, datetime

CALENDAR_SUMMARY = 'TawzeefAI interview'
EMAIL_SUBJECT = 'Interview confirmation'


def _times(start: str, end: str) -> tuple[str, str]:
    try:
        first = datetime.fromisoformat(start)
        last = datetime.fromisoformat(end)
        if first.tzinfo is None or last.tzinfo is None or first.utcoffset() is None or last.utcoffset() is None:
            raise ValueError('Timezone required')
        if last <= first:
            raise ValueError('Invalid interval')
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError('An ordered timezone-aware interview interval is required') from exc
    return tuple(value.astimezone(UTC).isoformat().replace('+00:00', 'Z') for value in (first, last))


def confirmation_content(start: str, end: str) -> tuple[str, str]:
    """Generate the exact message text for both local review and dispatch."""
    start, end = _times(start, end)
    return EMAIL_SUBJECT, f'Your interview is scheduled from {start} to {end}.\n'
