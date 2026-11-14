"""Narrow synthetic privacy and counterfactual checks, not a fairness audit."""
import io

from docx import Document
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def resume(name, gender):
    doc = Document()
    doc.add_paragraph(f'Name: {name}')
    doc.add_paragraph(f'Gender: {gender}')
    doc.add_paragraph('Synthetic independent evidence: implemented Python services for a sample project.')
    data = io.BytesIO()
    doc.save(data)
    return data.getvalue()


def test_two_labeled_identity_variants_and_checkpoint_storage(mode):  # noqa: F811
    """This pair tests only labeled fields and cannot prove general blindness."""
    with TestClient(create_app(seed_demo=True)) as first, TestClient(create_app(seed_demo=True)) as second, TestClient(create_app(seed_demo=True)) as hr:
        login(first, 'candidate')
        login(second, 'candidate2')
        login(hr, 'hr')
        job = first.get('/api/jobs').json()[0]['id']
        with db_for(mode) as db:
            db.execute('UPDATE jobs SET required_skills=%s,preferred_skills=%s WHERE id=%s', ('["Python", "Rust"]', '[]', job))
        previews, drafts = [], []
        for client, name, gender in ((first, 'Synthetic Person Alpha', 'woman'), (second, 'Synthetic Person Beta', 'man')):
            app_response = client.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume(name, gender))
            assert app_response.status_code == 201, app_response.text
            app_id = app_response.json()['id']
            created = hr.post(f'/api/applications/{app_id}/adaptive-review-preparations', json={'keyword_preview': True})
            assert created.status_code == 201, created.text
            draft = created.json()['id']
            drafts.append(draft)
            assert hr.post(f'/api/graph-review-drafts/{draft}/prepare-all').status_code == 200
            opened = hr.get(f'/api/graph-review-drafts/{draft}')
            assert opened.status_code == 200
            previews.append(opened.json()['result']['matching']['keyword_preview'])
        assert previews[0] == previews[1]
        assert previews[0]['required'][0]['mentioned'] and not previews[0]['required'][1]['mentioned']
        assert 'Python services' in previews[0]['required'][0]['evidence'][0]

        # The graph saver may store channels in inline JSON, binary blobs, or
        # pending writes. Search all three for these exact synthetic markers.
        markers = [b'Synthetic Person Alpha', b'Synthetic Person Beta',
                   b'Synthetic independent evidence', b'Python services', b'Gender: woman', b'Gender: man']
        with db_for(mode) as db:
            for draft in drafts:
                thread = f'graph-review-{draft}'
                rows = db.execute('SELECT checkpoint::text AS payload FROM checkpoints WHERE thread_id=%s', (thread,)).fetchall()
                blobs = db.execute('SELECT blob FROM checkpoint_blobs WHERE thread_id=%s', (thread,)).fetchall()
                writes = db.execute('SELECT blob FROM checkpoint_writes WHERE thread_id=%s', (thread,)).fetchall()
                assert rows and blobs
                values = [row['payload'].encode() for row in rows]
                values += [bytes(row['blob']) for row in blobs + writes if row['blob'] is not None]
                for marker in markers:
                    assert all(marker not in value for value in values), f'Unexpected synthetic marker in {thread}'
