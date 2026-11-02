"""HR metrics computed from persisted application and audit records."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role

router = APIRouter(prefix="/api")
HR = Annotated[User, Depends(require_roles("hr"))]


@router.get('/dashboard/screenings/daily')
def daily_screenings(request: Request, user: HR,
                     days: Annotated[int, Query(ge=1, le=90)] = 30,
                     end_date: date | None = None):
    """Count retained applications and screening events by UTC calendar day."""
    end = end_date or datetime.now(UTC).date()
    try:
        start = end - timedelta(days=days - 1)
    except OverflowError:
        raise HTTPException(status_code=422, detail='Date range is outside the supported calendar.') from None
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        rows = db.execute("""SELECT date(e.created_at) AS day, COUNT(*) AS events,
                   COUNT(DISTINCT e.application_id) AS applications
            FROM audit_events e JOIN applications a ON a.id=e.application_id
            WHERE e.action='screened' AND date(e.created_at) BETWEEN ? AND ?
            GROUP BY date(e.created_at) ORDER BY day""",
                          (start.isoformat(), end.isoformat())).fetchall()
        _require_current_role(db, request, user, 'hr')
    counts = {row['day']: (row['events'], row['applications']) for row in rows}
    result = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        events, applications = counts.get(day, (0, 0))
        result.append({'date': day, 'screening_events': events, 'applications_screened': applications})
    return {'timezone': 'UTC', 'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'definition': ('Recorded screened audit events for retained applications, by UTC day. '
                           'Repeated screenings count as separate events, but each application counts once per day. '
                           'This is screening activity, not a measure of HR decisions or applicant quality.'),
            'days': result}


def _milestone_counts(applications, events):
    """Count each retained application once per explicitly recorded milestone."""
    retained = {row['id'] for row in applications}
    actions = ('screened', 'interview_scheduled', 'interview_completed', 'interview_cancelled', 'offer_presented',
               'offer_accepted', 'offer_declined', 'offer_withdrawn')
    seen = {name: set() for name in (*actions, 'advanced', 'hired', 'rejected')}
    for event in events:
        app_id, action = event['application_id'], event['action']
        if app_id not in retained:
            continue
        if action in actions:
            seen[action].add(app_id)
        elif action == 'decision':
            try:
                details = json.loads(event['details'])
                decision = details.get('decision') if isinstance(details, dict) else None
            except (TypeError, ValueError):
                continue
            label = {'advance': 'advanced', 'hire': 'hired', 'reject': 'rejected'}.get(decision) if isinstance(decision, str) else None
            if label:
                seen[label].add(app_id)
    return {name: len(ids) for name, ids in seen.items()}


@router.get('/dashboard/jobs')
def dashboard_by_job(request: Request, user: HR):
    """Describe current job pipelines and their recorded screening activity."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        jobs = db.execute('SELECT id,title FROM jobs ORDER BY title,id').fetchall()
        apps = db.execute('SELECT id,job_id,status,created_at FROM applications').fetchall()
        screenings = db.execute('''SELECT a.job_id, COUNT(*) AS n FROM audit_events e
            JOIN applications a ON a.id=e.application_id WHERE e.action='screened' GROUP BY a.job_id''').fetchall()
        decisions = db.execute('''SELECT e.application_id,e.details,e.created_at FROM audit_events e
            JOIN applications a ON a.id=e.application_id
            WHERE e.action='decision' AND a.status='hired' ORDER BY e.id DESC''').fetchall()
        _require_current_role(db, request, user, 'hr')

    latest_hire = {}
    for decision in decisions:
        try:
            if json.loads(decision['details']).get('decision') == 'hire':
                latest_hire.setdefault(decision['application_id'], decision['created_at'])
        except (ValueError, TypeError, AttributeError):
            continue
    counts = {row['job_id']: row['n'] for row in screenings}
    groups = {job['id']: {'job_id': job['id'], 'title': job['title'], 'applications_total': 0,
                          'screening_events': counts.get(job['id'], 0), 'statuses': {}, 'durations': []}
              for job in jobs}
    for application in apps:
        group = groups.get(application['job_id'])
        if group is None:
            continue
        group['applications_total'] += 1
        status = application['status']
        group['statuses'][status] = group['statuses'].get(status, 0) + 1
        if status == 'hired' and application['id'] in latest_hire:
            try:
                elapsed = (datetime.fromisoformat(latest_hire[application['id']]) -
                           datetime.fromisoformat(application['created_at'])).total_seconds() / 86400
                if elapsed >= 0:
                    group['durations'].append(elapsed)
            except (ValueError, TypeError, OverflowError):
                continue
    output = []
    for group in groups.values():
        durations = group.pop('durations')
        statuses = group.pop('statuses')
        group['pipeline'] = [{'status': status, 'count': count} for status, count in sorted(statuses.items())]
        group['time_to_hire'] = {'average_days': round(sum(durations) / len(durations), 2) if durations else None,
                                 'count': len(durations)}
        output.append(group)
    return {'jobs': output,
            'definition': 'Screenings count recorded events for each job’s current applications. '
                          'Time to hire is days from application submission to the latest recorded hire decision '
                          'for currently hired applications; invalid or negative durations are excluded.'}


@router.get("/dashboard")
def dashboard(request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        # All indicators must describe the same committed database snapshot.
        db.execute("BEGIN")
        apps = db.execute("SELECT id, status, created_at FROM applications").fetchall()
        screenings = db.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'screened'").fetchone()[0]
        decisions = db.execute("SELECT application_id, details, created_at FROM audit_events WHERE action = 'decision' ORDER BY id DESC").fetchall()
        milestone_events = db.execute("""SELECT application_id, action, details FROM audit_events
            WHERE action IN ('screened', 'decision', 'interview_scheduled', 'interview_completed', 'interview_cancelled',
                             'offer_presented', 'offer_accepted', 'offer_declined', 'offer_withdrawn')""").fetchall()

    # For an application currently hired, use its latest recorded hire decision.
    hire_times = {}
    for decision in decisions:
        try:
            if json.loads(decision["details"]).get("decision") == "hire":
                hire_times.setdefault(decision["application_id"], decision["created_at"])
        except (ValueError, AttributeError):
            continue
    by_status: dict[str, int] = {}
    elapsed = []
    for app in apps:
        by_status[app["status"]] = by_status.get(app["status"], 0) + 1
        if app["status"] == "hired" and app["id"] in hire_times:
            try:
                submitted = datetime.fromisoformat(app["created_at"])
                hired = datetime.fromisoformat(hire_times[app["id"]])
                days = (hired - submitted).total_seconds() / 86400
                if days >= 0:
                    elapsed.append(days)
            except (ValueError, TypeError):
                continue
    milestones = _milestone_counts(apps, milestone_events)
    return {
        "applications_total": len(apps),
        "screening_events": screenings,
        "pipeline": [{"status": status, "count": count} for status, count in sorted(by_status.items())],
        "recorded_milestones": [
            {"stage": "received", "count": len(apps)},
            *[{"stage": name, "count": milestones[name]} for name in
              ('screened', 'advanced', 'interview_scheduled', 'interview_completed', 'offer_presented', 'hired')],
        ],
        "recorded_outcomes": [{"outcome": name, "count": milestones[name]} for name in
                              ('interview_cancelled', 'offer_accepted', 'offer_declined', 'offer_withdrawn', 'rejected')],
        "time_to_hire": {
            "average_days": round(sum(elapsed) / len(elapsed), 2) if elapsed else None,
            "count": len(elapsed),
            "definition": "Days from application submission to latest recorded hire decision for currently hired applications; invalid or negative durations excluded.",
        },
        "calendar": "local-only",
    }
