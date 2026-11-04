"""HR-reviewed offer graph uses an owner-bound draft and one local transaction."""
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path):
    path = tmp_path / 'offer-graph.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','fictional','advanced')", (candidate,)).lastrowid
            interviews = []
            for index in range(2):
                interview = db.execute("INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by) VALUES (?,?,'2020-01-01T10:00:00Z','2020-01-01T11:00:00Z',?,?,'scheduled',?)", (application, candidate, f'Interviewer {index}', f'interviewer-{index}', hr)).lastrowid
                db.execute('INSERT INTO interview_completions(interview_id,recorded_by) VALUES(?,?)', (interview,hr))
                interviews.append(interview)
        assert client.post('/api/auth/login', json={'username':'hr','password':'hr-demo-only'}).status_code == 200
        yield client,path,application,interviews,candidate,hr


def begin(client, app):
    response=client.post('/api/graph-reviews',json={'intent':'present_offer','application_id':app,'offer_details':'Synthetic offer terms 487'})
    assert response.status_code == 201, response.text
    data=response.json()
    assert data['awaiting_review'] and data['state']['offer_preview']['details']=='Synthetic offer terms 487'
    return data


def decide(client, review, approved=True):
    body={'reviewed':approved}
    if approved:
        body['approval_fingerprint']=review['state']['action_proposal']['approval_fingerprint']
    return client.post(f"/api/graph-reviews/{review['id']}/resume",json=body)


def test_offer_review_approval_restart_and_privacy(case):
    client,path,app,_,candidate,_=case
    review=begin(client,app)
    preview = review['state']['offer_preview']
    with connect(path) as db:
        person = db.execute('SELECT username,display_name FROM users WHERE id=?', (candidate,)).fetchone()
        job = db.execute('SELECT id,title FROM jobs WHERE id=(SELECT job_id FROM applications WHERE id=?)', (app,)).fetchone()
    assert (preview['candidate_id'], preview['candidate_username'], preview['candidate_name']) == (candidate, person['username'], person['display_name'])
    assert (preview['job_id'], preview['job_title']) == (job['id'], job['title'])
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['offer_preview']['details']=='Synthetic offer terms 487'
    with connect(path) as db:
        for table in ('checkpoints','writes'):
            for row in db.execute(f'SELECT * FROM {table}'):
                assert all('Synthetic offer terms 487' not in str(value) for value in row)
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0]==0
    result=decide(client,review)
    assert result.status_code==200, result.text
    assert result.json()['state']['action_result']['status']=='offer_presented'
    assert decide(client,review).status_code==409
    with connect(path) as db:
        offer = db.execute('SELECT details, recipient_id FROM job_offers').fetchone()
        assert (offer['details'], offer['recipient_id']) == ('Synthetic offer terms 487', candidate)
        assert db.execute('SELECT count(*) FROM graph_offer_drafts').fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='offer_presented'").fetchone()[0]==1
        assert db.execute('SELECT status FROM applications WHERE id=?',(app,)).fetchone()[0]=='offered'


@pytest.mark.parametrize('field', ['candidate_name', 'candidate_username', 'job_title'])
def test_changed_preview_identity_invalidates_offer(case, field):
    client, path, app, _, candidate, _ = case
    review = begin(client, app)
    with connect(path) as db:
        if field == 'candidate_name':
            db.execute("UPDATE users SET display_name='Different name' WHERE id=?", (candidate,))
        elif field == 'candidate_username':
            db.execute("UPDATE users SET username='different-candidate' WHERE id=?", (candidate,))
        else:
            db.execute("UPDATE jobs SET title='Different role' WHERE id=(SELECT job_id FROM applications WHERE id=?)", (app,))
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['offer_preview']['status'] == 'stale'
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0] == 0


def test_decline_and_expiry_leave_no_offer(case):
    client,path,app,_,_,_=case
    review=begin(client,app)
    assert decide(client,review,False).json()['state']['action_result']['status']=='declined'
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM graph_offer_drafts').fetchone()[0]==0
    review=begin(client,app)
    with connect(path) as db:
        db.execute('UPDATE graph_offer_drafts SET expires_at=0 WHERE thread_id=?',(review['id'],))
    assert decide(client,review).status_code==409
    assert decide(client,review,False).json()['state']['action_result']['status']=='declined'
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM graph_offer_drafts').fetchone()[0]==0


def test_changed_terms_cannot_be_presented_with_old_approval(case):
    client, path, app, _, _, _ = case
    review = begin(client, app)
    with connect(path) as db:
        db.execute("UPDATE graph_offer_drafts SET details='Different terms' WHERE thread_id=?", (review['id'],))
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['offer_preview']['status'] == 'stale'
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0] == 0


@pytest.mark.parametrize('kind',['interview_move','completion_move','owner_revert','status_revert'])
def test_changed_and_reverted_context_cannot_authorize(case,kind):
    client,path,app,interviews,candidate,hr=case
    review=begin(client,app)
    with connect(path) as db:
        other=db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','other.pdf','fictional','advanced')",(candidate,)).lastrowid
        if kind=='interview_move':
            db.execute('UPDATE interviews SET application_id=? WHERE id=?',(other,interviews[0]))
        elif kind=='completion_move':
            third=db.execute("INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by) VALUES (?,?,'2020-01-01T10:00:00Z','2020-01-01T11:00:00Z','Other','other','scheduled',?)", (other,candidate,hr)).lastrowid
            db.execute('UPDATE interview_completions SET interview_id=? WHERE interview_id=?',(third,interviews[0]))
        elif kind=='owner_revert':
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?',(hr,app))
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?',(candidate,app))
        else:
            db.execute("UPDATE applications SET status='submitted' WHERE id=?",(app,))
            db.execute("UPDATE applications SET status='advanced' WHERE id=?",(app,))
    assert decide(client,review).status_code==409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0]==0


def test_non_hr_and_revoked_session_cannot_use_graph(case):
    client,path,app,_,_,_=case
    review=begin(client,app)
    assert client.post('/api/auth/logout').status_code==204
    assert decide(client,review).status_code==401
    assert client.post('/api/auth/login',json={'username':'candidate','password':'candidate-demo-only'}).status_code==200
    assert client.post('/api/graph-reviews',json={'intent':'present_offer','application_id':app,'offer_details':'Synthetic'}).status_code==403
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0]==0


def test_offer_eligibility_rejects_prior_candidate_interviews(case):
    client,path,app,_,_,hr=case
    with connect(path) as db:
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?',(hr,app))
    direct=client.post(f'/api/applications/{app}/offers',json={'details':'Synthetic'})
    graph=client.post('/api/graph-reviews',json={'intent':'present_offer','application_id':app,'offer_details':'Synthetic'})
    assert direct.status_code==409
    assert graph.status_code==409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM graph_offer_drafts').fetchone()[0]==0


@pytest.mark.parametrize('field,value', [('active', 0), ('role', 'employee')])
def test_offer_requires_active_candidate_account(case, field, value):
    client, path, app, _, candidate, _ = case
    with connect(path) as db:
        db.execute(f'UPDATE users SET {field}=? WHERE id=?', (value, candidate))
    assert client.post(f'/api/applications/{app}/offers', json={'details': 'Synthetic'}).status_code == 409
    assert client.post('/api/graph-reviews', json={'intent': 'present_offer', 'application_id': app,
                                                   'offer_details': 'Synthetic'}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0] == 0


@pytest.mark.parametrize('field,value,original', [('active', 0, 1), ('role', 'employee', 'candidate')])
def test_account_change_and_restore_invalidates_offer_approval(case, field, value, original):
    client, path, app, _, candidate, _ = case
    review = begin(client, app)
    with connect(path) as db:
        db.execute(f'UPDATE users SET {field}=? WHERE id=?', (value, candidate))
        db.execute(f'UPDATE users SET {field}=? WHERE id=?', (original, candidate))
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['offer_preview']['status'] == 'stale'
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0] == 0
