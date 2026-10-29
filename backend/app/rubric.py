"""Human-reviewed, evidence-linked weighted assessment without CV text.

This calculator deliberately does not parse CVs or make hiring decisions.
Only structured reviewer judgments and opaque local references are accepted.
It cannot verify that a reviewer actually inspected an evidence source.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

WEIGHTS = {
    'mandatory': Decimal(40),
    'preferred': Decimal(15),
    'experience': Decimal(20),
    'projects': Decimal(15),
    'education': Decimal(10),
}


class Claim(BaseModel):
    """One criterion, assessed by a human against references in a local store.

    An opaque ``criterion_ref`` identifies a rubric criterion; ``evidence_refs``
    identify reviewed evidence or reviewer attestations, never raw document text.
    ``missing`` is an absence of demonstrated evidence, not proof of inability.
    """

    model_config = ConfigDict(extra='forbid')
    criterion_ref: str = Field(pattern=r'^c-[1-9][0-9]{0,3}$')
    status: Literal['met', 'missing', 'unknown']
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    reviewed_by_hr: StrictBool = False
    absence_review_attested: StrictBool = False

    @field_validator('evidence_refs')
    @classmethod
    def valid_refs(cls, value: list[str]) -> list[str]:
        import re

        if any(not re.fullmatch(r'e-[1-9][0-9]{0,3}', ref) for ref in value):
            raise ValueError('Evidence references must use the e-N format')
        if len(value) != len(set(value)):
            raise ValueError('Duplicate evidence references are not allowed')
        return value

    @model_validator(mode='after')
    def require_reviewed_evidence(self):
        if self.status != 'unknown' and (not self.reviewed_by_hr or not self.evidence_refs):
            raise ValueError('A met or missing assessment needs HR review and evidence references')
        if self.status == 'missing' and not self.absence_review_attested:
            raise ValueError('A missing judgment requires an explicit review of the available source')
        if self.status != 'missing' and self.absence_review_attested:
            raise ValueError('Absence attestation applies only to missing judgments')
        return self


class Assessment(BaseModel):
    """Each list describes the criteria defined for one rubric category.

    An empty category is unknown and contributes its full weight to the upper
    bound. Category scores assume equally weighted criteria within a category.
    """

    model_config = ConfigDict(extra='forbid')
    mandatory: list[Claim] = Field(default_factory=list, max_length=50)
    preferred: list[Claim] = Field(default_factory=list, max_length=50)
    experience: list[Claim] = Field(default_factory=list, max_length=50)
    projects: list[Claim] = Field(default_factory=list, max_length=50)
    education: list[Claim] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def unique_criteria(self):
        criteria = [claim.criterion_ref for category in WEIGHTS for claim in getattr(self, category)]
        if len(criteria) != len(set(criteria)):
            raise ValueError('Criterion references must be unique across the assessment')
        return self


def _rounded(value: Decimal) -> float:
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def calculate_rubric(assessment: Assessment) -> dict:
    """Return points and uncertainty bounds with no automatic final decision.

    For each category, met contributes its equal fraction of that category's
    weight, missing contributes zero, and unknown may contribute zero to its
    maximum fraction. Unspecified categories are entirely unknown. A complete
    result still requires human review; a score is not a hiring decision.
    """
    if not isinstance(assessment, Assessment):
        raise TypeError('assessment must be an Assessment')
    components = []
    total_min = Decimal(0)
    total_max = Decimal(0)
    weighted_coverage = Decimal(0)
    missing_mandatory = []
    unknown_mandatory = []
    for category, weight in WEIGHTS.items():
        claims = getattr(assessment, category)
        count = len(claims)
        met = sum(claim.status == 'met' for claim in claims)
        missing = sum(claim.status == 'missing' for claim in claims)
        unknown = sum(claim.status == 'unknown' for claim in claims)
        if count:
            minimum = weight * Decimal(met) / Decimal(count)
            maximum = weight * Decimal(met + unknown) / Decimal(count)
            covered = weight * Decimal(met + missing) / Decimal(count)
        else:
            minimum, maximum, covered = Decimal(0), weight, Decimal(0)
        total_min += minimum
        total_max += maximum
        weighted_coverage += covered
        if category == 'mandatory':
            missing_mandatory = [claim.criterion_ref for claim in claims if claim.status == 'missing']
            unknown_mandatory = [claim.criterion_ref for claim in claims if claim.status == 'unknown']
        components.append({
            'category': category,
            'weight': int(weight),
            'minimum_points': _rounded(minimum),
            'maximum_points': _rounded(maximum),
            'covered_weight': _rounded(covered),
            'counts': {'met': met, 'missing': missing, 'unknown': unknown},
            'claims': [claim.model_dump() for claim in claims],
            'unconfigured': not bool(claims),
        })
    return {
        'rubric_version': 'human-reviewed-v1',
        'components': components,
        'minimum_score': _rounded(total_min),
        'maximum_score': _rounded(total_max),
        'coverage_percent': _rounded(weighted_coverage),
        'complete': weighted_coverage == Decimal(100),
        'mandatory_flags': {'missing': missing_mandatory, 'unknown': unknown_mandatory},
        'requires_human_review': True,
        'notice': 'Scores summarize reviewed evidence only; unknowns and missing evidence are not automatic rejections.',
    }
