"""Validate the internal shape and references of synthetic capstone samples."""

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def unique(items, key="id"):
    values = [item[key] for item in items]
    assert len(values) == len(set(values)), f"Duplicate {key}"
    return set(values)


def validate():
    jobs = read("jobs.json")["jobs"]
    profiles = read("cvs.json")["profiles"]
    sections = read("handbook.json")["sections"]
    cases = read("policy-qa.json")["cases"]
    patterns = read("hire-patterns.json")["patterns"]
    negative = read("negative-qa.json")["cases"]
    ranking = read("ranking.json")["cases"]
    assert 5 <= len(jobs) <= 8 and len(profiles) == 30 and len(cases) == 30
    assert len(sections) == 15 and len(patterns) == len(jobs)
    job_ids = unique(jobs)
    unique(profiles)
    unique(cases)
    unique(patterns)
    unique(negative)
    assert len(ranking) == 10
    unique(ranking)
    section_ids = unique(sections)
    by_section = {section["id"]: section for section in sections}
    by_profile = {profile["id"]: profile for profile in profiles}
    assert len({section["logical_page"] for section in sections}) == len(sections)
    assert Counter(profile["language"] for profile in profiles) == {"en": 18, "ar": 12}
    assert Counter(case["language"] for case in cases) == {"en": 15, "ar": 15}
    for job in jobs:
        assert job["required_skills"] and job["responsibilities"]
    for profile in profiles:
        assert profile["role_id"] in job_ids and profile["resume"].strip()
    for pattern in patterns:
        assert pattern["role_id"] in job_ids and pattern["audience"] == "hr"
    for section in sections:
        assert section["document_id"] == "cap-handbook-v1" and section["audience"] == "staff"
        assert section["text_en"].strip() and section["text_ar"].strip()
    for case in cases:
        assert case["question"].strip() and case["expected_answer"].strip()
        assert set(case["expected_section_ids"]).issubset(section_ids)
        assert case["expected_section_ids"]
        assert all(by_section[ref]["logical_page"] == case["expected_logical_page"] for ref in case["expected_section_ids"])
    for case in negative:
        assert case["expected"] in {"abstain", "deny_or_abstain"}
        assert set(case["forbidden_section_ids"]).issubset(section_ids)
    for case in ranking:
        assert case["role_id"] in job_ids and len(case["profile_ids"]) == 2
        assert len(set(case["profile_ids"])) == 2
        assert case["expected_more_documented"] in case["profile_ids"]
        assert case["rationale"].strip()
        assert all(ref in by_profile and by_profile[ref]["role_id"] == case["role_id"] for ref in case["profile_ids"])
    print(f"Validated: {len(jobs)} jobs, {len(profiles)} fictional CVs, {len(sections)} logical sections, {len(cases)} policy questions, {len(patterns)} fictional patterns, {len(ranking)} paired comparisons.")
    print("Reference labels are synthetic drafts; no human approval is claimed.")


if __name__ == "__main__":
    validate()
