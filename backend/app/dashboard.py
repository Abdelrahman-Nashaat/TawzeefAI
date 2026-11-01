"""HR metrics computed from persisted application and audit records."""

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role

router = APIRouter(prefix="/api")
HR = Annotated[User, Depends(require_roles("hr"))]


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
    return {
        "applications_total": len(apps),
        "screening_events": screenings,
        "pipeline": [{"status": status, "count": count} for status, count in sorted(by_status.items())],
        "time_to_hire": {
            "average_days": round(sum(elapsed) / len(elapsed), 2) if elapsed else None,
            "count": len(elapsed),
            "definition": "Days from application submission to latest recorded hire decision for currently hired applications; invalid or negative durations excluded.",
        },
        "calendar": "local-only",
    }
