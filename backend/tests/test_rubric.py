import json

import pytest
from pydantic import ValidationError

from app.rubric import Assessment, calculate_rubric


def reviewed(number, status='met'):
    return {'criterion_ref': f'c-{number}', 'status': status,
            'evidence_refs': [f'e-{number}'], 'reviewed_by_hr': True,
            'absence_review_attested': status == 'missing'}


def test_complete_weighted_rubric_and_mandatory_flag():
    assessment = Assessment(
        mandatory=[reviewed(1), reviewed(2, 'missing')],
        preferred=[reviewed(3)], experience=[reviewed(4)],
        projects=[reviewed(5, 'missing')], education=[reviewed(6)],
    )
    result = calculate_rubric(assessment)
    assert [item['weight'] for item in result['components']] == [40, 15, 20, 15, 10]
    assert result['minimum_score'] == result['maximum_score'] == 65
    assert result['coverage_percent'] == 100 and result['complete']
    assert result['mandatory_flags']['missing'] == ['c-2']
    assert result['requires_human_review'] is True


def test_unknown_and_empty_categories_yield_range_not_invented_score():
    result = calculate_rubric(Assessment(mandatory=[reviewed(1), {'criterion_ref': 'c-2', 'status': 'unknown'}]))
    assert result['minimum_score'] == 20 and result['maximum_score'] == 100
    assert result['coverage_percent'] == 20 and result['complete'] is False
    assert result['mandatory_flags']['unknown'] == ['c-2']
    empty = calculate_rubric(Assessment())
    assert (empty['minimum_score'], empty['maximum_score'], empty['coverage_percent']) == (0, 100, 0)
    assert all(component['unconfigured'] for component in empty['components'])


@pytest.mark.parametrize('claim', [
    {'criterion_ref': 'c-1', 'status': 'met'},
    {'criterion_ref': 'c-1', 'status': 'missing', 'reviewed_by_hr': True},
    {'criterion_ref': 'c-1', 'status': 'met', 'evidence_refs': ['e-1']},
    {'criterion_ref': 'c-1', 'status': 'met', 'reviewed_by_hr': 'yes', 'evidence_refs': ['e-1']},
    {'criterion_ref': 'c-1', 'status': 'missing', 'reviewed_by_hr': True, 'evidence_refs': ['e-1']},
    {'criterion_ref': 'c-1', 'status': 'missing', 'reviewed_by_hr': True, 'evidence_refs': ['e-1'], 'absence_review_attested': 'true'},
])
def test_met_and_missing_require_human_review_and_evidence(claim):
    with pytest.raises(ValidationError):
        Assessment(mandatory=[claim])


def test_rejects_resume_text_and_personal_identifiers():
    with pytest.raises(ValidationError):
        Assessment(mandatory=[{**reviewed(1), 'name': 'Synthetic Name'}])
    with pytest.raises(ValidationError):
        Assessment(mandatory=[{**reviewed(1), 'evidence_refs': ['person@example.com']}])
    with pytest.raises(ValidationError):
        Assessment(mandatory=[{**reviewed(1), 'criterion_ref': 'Synthetic Name'}])
    with pytest.raises(ValidationError):
        Assessment(mandatory=[reviewed(1)], resume_text='private')
    assert 'example.com' not in json.dumps(calculate_rubric(Assessment(mandatory=[reviewed(1)])))


def test_duplicate_criteria_and_invalid_status_are_rejected():
    with pytest.raises(ValidationError):
        Assessment(mandatory=[reviewed(1)], preferred=[reviewed(1)])
    with pytest.raises(ValidationError):
        Assessment(mandatory=[reviewed(1, 'approve')])
