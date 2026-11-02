"""Real opt-in OCR test on a synthetic English and Arabic scanned document."""

import base64
import os
import subprocess
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.knowledge import _document_passages
from app.main import create_app
from app.ocr_worker import _languages_available


def test_language_list_header_may_use_a_non_utf8_path(monkeypatch):
    monkeypatch.setattr('app.ocr_worker.subprocess.run', lambda *a, **k:
                        SimpleNamespace(stdout=b'List of languages in C:\\data\\\xe3:\neng\nara\n'))
    assert _languages_available('tesseract', 'eng+ara')
    assert not _languages_available('tesseract', 'eng+fra')


@pytest.mark.skipif(os.name != 'nt' or os.environ.get('TAWZEEF_TEST_REAL_BILINGUAL_OCR') != '1',
                    reason='Requires Windows, Tesseract, and both local language models')
def test_real_bilingual_scanned_uploads(tmp_path, monkeypatch):
    from PIL import Image

    png = tmp_path / 'synthetic.png'
    output = str(png).replace("'", "''")
    drawing = f"""
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap 1800, 700
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([System.Drawing.Color]::White)
$font = New-Object System.Drawing.Font 'Arial', 75, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
$graphics.DrawString('PYTHON DEVELOPER', $font, [System.Drawing.Brushes]::Black, 100, 120)
$format = New-Object System.Drawing.StringFormat
$format.FormatFlags = [System.Drawing.StringFormatFlags]::DirectionRightToLeft
$rectangle = New-Object System.Drawing.RectangleF 100, 220, 800, 120
$graphics.DrawString('مهندس برمجيات', $font, [System.Drawing.Brushes]::Black, $rectangle, $format)
$rectangle2 = New-Object System.Drawing.RectangleF 100, 320, 800, 120
$graphics.DrawString('مهندس برمجيات', $font, [System.Drawing.Brushes]::Black, $rectangle2, $format)
$bitmap.Save('{output}', [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
"""
    command = base64.b64encode(drawing.encode('utf-16le')).decode('ascii')
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    scanned = BytesIO()
    with Image.open(png) as image:
        image.convert('RGB').save(scanned, format='PDF', resolution=150)

    monkeypatch.setenv('TAWZEEF_ENABLE_OCR', '1')
    app = create_app(tmp_path / 'bilingual.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        response = client.post('/api/applications', params={'job_id': 'job-backend-python',
            'filename': 'synthetic.pdf', 'ocr': 'true', 'ocr_language': 'eng+ara'}, content=scanned.getvalue())
        assert response.status_code == 201, response.text
        application = response.json()
        assert application['extraction']['ocr_pages'] == [1]
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        applications = client.get('/api/applications').json()
        uploaded = next(item for item in applications if item['id'] == application['id'])
        assert 'PYTHON' in uploaded['resume_text'].upper()
        assert 'مهندس' in uploaded['resume_text']
        response = client.post('/api/knowledge/documents', params={'title': 'Synthetic bilingual document',
            'audience': 'staff', 'filename': 'synthetic.pdf', 'ocr': 'true', 'ocr_language': 'eng+ara'},
            content=scanned.getvalue())
        assert response.status_code == 201, response.text
        assert response.json()['ocr_pages'] == [1]
        with connect(app.state.database_path) as db:
            document = dict(db.execute('SELECT * FROM knowledge_documents WHERE id=?', (response.json()['id'],)).fetchone())
        sources = _document_passages(document)
        assert sources and sources[0].extraction == 'ocr'
        assert 'PYTHON' in document['text'].upper()
        assert 'مهندس' in document['text']
