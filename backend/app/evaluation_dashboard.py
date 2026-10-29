"""Run the bundled synthetic evaluation for an authenticated HR reviewer."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth import User, require_roles
from evaluate_capstone import evaluate

router = APIRouter(prefix='/api/evaluation')
HR = Annotated[User, Depends(require_roles('hr'))]


@router.get('/capstone')
def capstone_evaluation(user: HR):
    report = evaluate()
    return {
        'summary': report['summary'],
        'settings': report['settings'],
        'disclaimer': report['disclaimer'],
        'missed_retrieval_cases': [row['id'] for row in report['retrieval'] if not row['source_recall_at_3']],
        'failed_negative_cases': [row['id'] for row in report['negative_checks'] if not row['passed']],
        'failed_ranking_cases': [row['id'] for row in report['ranking'] if not row['passed']],
    }
