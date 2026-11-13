"""HR-only interview prompts based on current audited source-bound reviews."""
import json

from fastapi.testclient import TestClient
from test_postgres_durable_graph import assess, create_application
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_guide_requires_review_and_rechecks_source_and_role(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        url = f'/api/applications/{app_id}/interview-guide'
        assert candidate.get(url).status_code == 403
        assert hr.get(url).status_code == 409
        assess(hr, app_id)
        response = hr.get(url)
        assert response.status_code == 200, response.text
        guide = response.json()
        assert guide['assessment_event_id'] > 0
        assert guide['questions']
        assert any(q['category'] == 'mandatory_skills' and q['reviewed'] and q['evidence_offsets'] for q in guide['questions'])
        assert any(q['category'] == 'education' and not q['reviewed'] and q['evidence_offsets'] is None for q in guide['questions'])
        assert 'Synthetic candidate built' not in response.text
        with db_for(mode) as db:
            db.execute('UPDATE applications SET resume_text=%s WHERE id=%s', ('Changed resume', app_id))
        assert hr.get(url).status_code == 409


def test_guide_omits_direct_attribute_labels_and_detects_tampered_review(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        app_id = create_application(candidate)
        login(hr, 'hr')
        with db_for(mode) as db:
            db.execute('''UPDATE jobs SET required_skills=%s WHERE id=(SELECT job_id FROM applications WHERE id=%s)''',
                       (json.dumps(['Python', 'Age']), app_id))
        assess(hr, app_id)
        url = f'/api/applications/{app_id}/interview-guide'
        guide = hr.get(url).json()
        assert any(q['criterion'] == 'Python' for q in guide['questions'])
        assert all(q['criterion'] != 'Age' for q in guide['questions'])
        assert guide['omitted_count'] >= 1
        with db_for(mode) as db:
            db.execute("UPDATE hr_rubric_history SET review_json='{}' WHERE application_id=%s", (app_id,))
        assert hr.get(url).status_code == 409
