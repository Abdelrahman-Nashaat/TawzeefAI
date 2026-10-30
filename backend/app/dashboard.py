"""HR metrics computed from persisted application and audit records."""

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.auth import User, require_roles
from app.db import connect

router = APIRouter(prefix="/api")
HR = Annotated[User, Depends(require_roles("hr"))]


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
