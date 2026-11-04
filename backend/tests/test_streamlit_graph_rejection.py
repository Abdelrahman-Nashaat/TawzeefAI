"""Exercise supervised rejection through Streamlit and the actual local API."""
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip('streamlit')
from streamlit.testing.v1 import AppTest

from app.db import connect
from app.main import create_app
from streamlit_client import LocalApi


@pytest.mark.parametrize('scenario', ['approve', 'discard', 'stale', 'changed', 'revoked', 'uncertain'])
def test_rejection_review_real_api(tmp_path, scenario):
    path = tmp_path / 'rejection.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as server:
        resumes = []

        def forward(request):
            if request.url.path.endswith('/resume'):
                resumes.append(request)
                if scenario == 'changed':
                    with connect(path) as db:
                        db.execute("UPDATE jobs SET title='Changed role' WHERE id='job-backend-python'")
                if scenario in ('revoked', 'uncertain'):
                    return httpx.Response(401 if scenario == 'revoked' else 503)
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            user = hr.request('POST', '/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
            with connect(path) as db:
                candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
                app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','synthetic.pdf','Python')", (candidate,)).lastrowid
                if scenario == 'approve':
                    hr_id = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
                    interview = db.execute("""INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by)
                        VALUES (?,?,'2099-01-01T10:00:00Z','2099-01-01T11:00:00Z','Synthetic interviewer','synthetic','scheduled',?)""", (app, candidate, hr_id)).lastrowid
                    slot = db.execute("""INSERT INTO interview_slots(start_at,end_at,interviewer,interviewer_key,created_by,target_application_id,target_candidate_id)
                        VALUES ('2099-01-02T10:00:00Z','2099-01-02T11:00:00Z','Synthetic interviewer','synthetic',?,?,?)""", (hr_id, app, candidate)).lastrowid
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=60)
            ui.session_state['local_api'] = hr
            ui.session_state['current_user'] = user
            ui.run()
            next(box for box in ui.selectbox if box.label == 'Review job').set_value('job-backend-python').run()
            next(text for text in ui.text_area if text.label == 'Reason for graph rejection').set_value('Synthetic reason')
            next(button for button in ui.button if button.label == 'Start graph rejection review').click().run()
            assert not ui.exception
            review_id = ui.session_state['graph_reject_review']['id']
            assert any(f'Candidate #{candidate}' in item.value for item in ui.text)
            assert any('Synthetic reason' in item.value for item in ui.text)
            assert any('Python Backend Developer' in item.value for item in ui.text)
            assert not any(button.label == 'Start graph rejection review' for button in ui.button)
            if scenario == 'approve':
                preview = hr.request('GET', f'/api/graph-reviews/{review_id}')['state']['rejection_preview']
                assert [item['id'] for item in preview['interviews']] == [interview]
                assert [item['id'] for item in preview['slots']] == [slot]
            with connect(path) as db:
                assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'submitted'
                for row in db.execute('SELECT * FROM checkpoints'):
                    assert all('Synthetic reason' not in str(value) for value in row)
            if scenario == 'stale':
                with connect(path) as db:
                    db.execute("UPDATE jobs SET title='Changed role' WHERE id='job-backend-python'")
                ui.run()
                assert not any(button.label == 'Approve graph rejection' for button in ui.button)
                assert any('context changed' in item.value for item in ui.warning)
            if scenario in ('discard', 'stale'):
                next(button for button in ui.button if button.label == 'Discard graph rejection review').click().run()
                assert any('Review discarded' in item.value for item in ui.success)
            else:
                next(button for button in ui.button if button.label == 'Approve graph rejection').click().run()
                assert not resumes
                assert any('Confirm that you reviewed' in item.value for item in ui.error)
                next(box for box in ui.checkbox if box.label == 'I reviewed the rejection reason and effects').check()
                next(button for button in ui.button if button.label == 'Approve graph rejection').click().run()
                assert len(resumes) == 1
            assert not ui.exception
            with connect(path) as db:
                status = db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0]
                count = db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (app,)).fetchone()[0]
                drafts = db.execute('SELECT count(*) FROM graph_rejection_drafts WHERE thread_id=?', (review_id,)).fetchone()[0]
            if scenario == 'approve':
                assert (status, count, drafts) == ('rejected', 1, 0)
                with connect(path) as db:
                    assert db.execute('SELECT status FROM interviews WHERE id=?', (interview,)).fetchone()[0] == 'cancelled'
                    assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (slot,)).fetchone()[0] == 1
                assert any('Rejection recorded locally' in item.value for item in ui.success)
            else:
                assert (status, count) == ('submitted', 0)
                if scenario in ('discard', 'stale'):
                    assert drafts == 0
                if scenario == 'revoked':
                    assert 'current_user' not in ui.session_state
                if scenario == 'uncertain':
                    assert any('could not be confirmed' in item.value for item in ui.warning)
        finally:
            hr.close()
