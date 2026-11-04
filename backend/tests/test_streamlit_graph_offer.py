"""Exercise graph offer approval in Streamlit through the local FastAPI app."""
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
def test_graph_offer_real_api(tmp_path, scenario):
    path = tmp_path / 'offer.sqlite3'
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
                hr_id = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
                app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','Python','advanced')", (candidate,)).lastrowid
                interview = db.execute("""INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by)
                    VALUES (?,?,'2020-01-01T10:00:00Z','2020-01-01T11:00:00Z','Synthetic interviewer','synthetic','scheduled',?)""", (app, candidate, hr_id)).lastrowid
                db.execute('INSERT INTO interview_completions(interview_id,recorded_by) VALUES (?,?)', (interview, hr_id))
            ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'streamlit_app.py'), default_timeout=60)
            ui.session_state['local_api'] = hr
            ui.session_state['current_user'] = user
            ui.run()
            next(box for box in ui.selectbox if box.label == 'Review job').set_value('job-backend-python').run()
            next(area for area in ui.text_area if area.label == 'Terms for graph offer').set_value('Synthetic offer terms')
            next(button for button in ui.button if button.label == 'Start graph offer review').click().run()
            assert not ui.exception
            review_id = ui.session_state['graph_offer_review']['id']
            assert any(f'Candidate #{candidate}' in item.value for item in ui.text)
            assert any('Synthetic offer terms' in item.value for item in ui.text)
            assert any('Python Backend Developer' in item.value for item in ui.text)
            assert not any(button.label == 'Start graph offer review' for button in ui.button)
            with connect(path) as db:
                for row in db.execute('SELECT * FROM checkpoints'):
                    assert all('Synthetic offer terms' not in str(value) for value in row)
            if scenario == 'stale':
                with connect(path) as db:
                    db.execute("UPDATE jobs SET title='Changed role' WHERE id='job-backend-python'")
                ui.run()
                assert not any(button.label == 'Approve graph offer' for button in ui.button)
                assert any('context changed' in item.value for item in ui.warning)
            if scenario in ('discard', 'stale'):
                next(button for button in ui.button if button.label == 'Discard graph offer review').click().run()
                assert any('Review discarded' in item.value for item in ui.success)
            else:
                next(button for button in ui.button if button.label == 'Approve graph offer').click().run()
                assert not resumes
                assert any('Confirm that you reviewed' in item.value for item in ui.error)
                next(box for box in ui.checkbox if box.label == 'I reviewed the candidate, role and offer terms').check()
                next(button for button in ui.button if button.label == 'Approve graph offer').click().run()
                assert len(resumes) == 1
            assert not ui.exception
            with connect(path) as db:
                offer = db.execute('SELECT details,recipient_id FROM job_offers WHERE application_id=?', (app,)).fetchone()
                status = db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0]
                drafts = db.execute('SELECT count(*) FROM graph_offer_drafts WHERE thread_id=?', (review_id,)).fetchone()[0]
            if scenario == 'approve':
                assert (offer['details'], offer['recipient_id'], status, drafts) == ('Synthetic offer terms', candidate, 'offered', 0)
                assert any('Offer presented locally' in item.value for item in ui.success)
            else:
                assert (offer, status) == (None, 'advanced')
                if scenario in ('discard', 'stale'):
                    assert drafts == 0
                if scenario == 'revoked':
                    assert 'current_user' not in ui.session_state
                if scenario == 'uncertain':
                    assert any('could not be confirmed' in item.value for item in ui.warning)
        finally:
            hr.close()
