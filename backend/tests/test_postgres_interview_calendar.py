"""Authenticated calendar export through the real PostgreSQL booking flow."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_calendar_ownership_utc_escaping_and_cancel(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as other:
        login(candidate, 'candidate')
        login(hr, 'hr')
        login(other, 'candidate2')
        job = candidate.get('/api/jobs').json()[0]['id']
        with db_for(mode) as db:
            db.execute('UPDATE jobs SET title=%s WHERE id=%s', ('Python, test; test\\value\nBEGIN:VEVENT', job))
        result = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
        assert result.status_code == 201
        application = result.json()['id']
        assert hr.post(f'/api/applications/{application}/screen').status_code == 200
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'advance'}).status_code == 200
        start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
        slot = hr.post('/api/slots', json={'start_at': start.isoformat(),
            'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Private interviewer'}).json()['id']
        result = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application})
        assert result.status_code == 201
        interview = result.json()['id']
        url = f'/api/interviews/{interview}/calendar'
        assert other.get(url).status_code == 404
        response = candidate.get(url)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/calendar')
        assert response.headers['cache-control'] == 'no-store'
        raw = response.content
        assert all(len(line) <= 75 for line in raw.split(b'\r\n'))
        text = raw.decode().replace('\r\n ', '')
        assert text.count('\r\nBEGIN:VEVENT\r\n') == 1
        assert 'Python\\, test\\; test\\\\value\\nBEGIN:VEVENT' in text
        assert f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}" in text
        assert 'Private interviewer' not in text and 'Synthetic candidate' not in text
        assert hr.get(url).content == raw
        assert hr.post(f'/api/interviews/{interview}/cancel').status_code == 200
        assert candidate.get(url).status_code == 409
        assert hr.get(url).status_code == 409


def test_calendar_rejects_fractional_interview_time(mode):  # noqa: F811
    from test_postgres_scheduling import ready_booking

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        response = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application})
        assert response.status_code == 201
        interview = response.json()['id']
        with db_for(mode) as db:
            db.execute("UPDATE interviews SET start_at=date_trunc('second',start_at) + interval '0.5 second' WHERE id=%s", (interview,))
        assert candidate.get(f'/api/interviews/{interview}/calendar').status_code == 409
