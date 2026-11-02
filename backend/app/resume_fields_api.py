"""Read-only structured résumé fields for authenticated HR reviewers."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role, get_application
from app.resume_fields import resume_fields_for_display
from app.resume_mask import masked_source

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]


@router.get('/applications/{application_id}/resume/fields')
def resume_fields(application_id: int, request: Request, user: HR):
    """Extract exact spans from the current partially redacted source."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        get_application(db, application_id)
        result = resume_fields_for_display(masked_source(db, application_id)['text'])
        _require_current_role(db, request, user, 'hr')
        return result
