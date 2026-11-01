"""Local OCR is opt-in, bounded and retains page provenance."""

import os
import subprocess
import sys
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from test_partial_pdf_ingestion import make_pdf

from app.document_text import DocumentTextError, _run_ocr_worker, extract_pdf_pages_with_ocr
from app.knowledge import _document_passages
from app.main import create_app


def test_ocr_bound_and_default_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_OCR', raising=False)
    with pytest.raises(DocumentTextError) as error:
        extract_pdf_pages_with_ocr('x.pdf', make_pdf(['image'] * 6))
    assert error.value.code == 'ocr_limit'
    app = create_app(tmp_path / 'ocr.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'})
        params = {'job_id': 'job-backend-python', 'filename': 'resume.pdf', 'ocr': 'true'}
        response = client.post('/api/applications', params=params, content=make_pdf(['image']))
        assert response.status_code == 422 and response.json()['detail']['code'] == 'ocr_disabled'
        assert client.get('/api/applications').json() == []


def test_upload_opt_in_role_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv('TAWZEEF_ENABLE_OCR', '1')
    pages = [{'page': 1, 'text': 'Synthetic native text'}, {'page': 2, 'text': 'Synthetic scanned text'}]
    monkeypatch.setattr('app.recruitment.extract_pdf_pages_with_ocr', lambda *a: (pages, [2]))
    monkeypatch.setattr('app.knowledge_documents.extract_pdf_pages_with_ocr', lambda *a: (pages, [2]))
    app = create_app(tmp_path / 'ocr.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'})
        params = {'job_id': 'job-backend-python', 'filename': 'resume.pdf', 'ocr': 'true'}
        response = client.post('/api/applications', params=params, content=b'synthetic')
        assert response.status_code == 201 and response.json()['extraction']['ocr_pages'] == [2]
        assert client.get('/api/applications').json()[0]['extraction']['ocr_pages'] == [2]
        document = {'title': 'Synthetic', 'audience': 'staff', 'filename': 'policy.pdf', 'ocr': 'true'}
        assert client.post('/api/knowledge/documents', params=document, content=b'synthetic').status_code == 403
        client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
        assert client.get('/api/applications').json()[0]['extraction']['ocr_pages'] == [2]
        response = client.post('/api/knowledge/documents', params=document, content=b'synthetic')
        assert response.status_code == 201 and response.json()['ocr_pages'] == [2]
        assert client.get('/api/knowledge/documents').json()[0]['ocr_pages'] == [2]


def test_knowledge_passages_retain_ocr_provenance():
    import json
    pages = [{'page': 1, 'text': 'Native page'}, {'page': 2, 'text': 'Scanned page'}]
    document = {'id': 1, 'title': 'Synthetic', 'text': 'Native page\nScanned page',
                'pages_json': json.dumps(pages), 'ocr_pages_json': '[2]', 'sections_json': None}
    sources = _document_passages(document)
    assert sources[0].extraction == 'native'
    assert sources[1].extraction == 'ocr' and 'approximate' in sources[1].extraction_notice


@pytest.mark.parametrize(('worker_result', 'expected'), [
    ((1, b''), 'ocr_unavailable'),
    ((0, b'[{"index":0,"text":""}]'), 'ocr_unavailable'),
    ((2, b''), 'ocr_language_unavailable'),
])
def test_ocr_worker_failures_do_not_store_partial_data(tmp_path, monkeypatch, worker_result, expected):
    monkeypatch.setenv('TAWZEEF_ENABLE_OCR', '1')
    monkeypatch.setattr('app.document_text._run_ocr_worker', lambda *args: worker_result)
    app = create_app(tmp_path / 'failure.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'})
        params = {'job_id': 'job-backend-python', 'filename': 'resume.pdf', 'ocr': 'true'}
        response = client.post('/api/applications', params=params, content=make_pdf(['image']))
        assert response.status_code == 422 and response.json()['detail']['code'] == expected
        assert client.get('/api/applications').json() == []
        client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
        params = {'title': 'Synthetic', 'audience': 'staff', 'filename': 'policy.pdf', 'ocr': 'true'}
        response = client.post('/api/knowledge/documents', params=params, content=make_pdf(['image']))
        assert response.status_code == 422 and response.json()['detail']['code'] == expected
        assert client.get('/api/knowledge/documents').json() == []


@pytest.mark.skipif(os.name != 'nt', reason='Tests Windows Job Object process-tree cleanup')
def test_worker_timeout_kills_real_descendant(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    marker = tmp_path / 'child.pid'
    script = tmp_path / 'worker.py'
    script.write_text('''import os, subprocess, sys, time\nfrom app.ocr_worker import contain_process_tree\n'''
        '''job = contain_process_tree()\nassert "SECRET_OCR_TEST" not in os.environ\n'''
        '''child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'''
        '''open(sys.argv[1], "w").write(str(child.pid))\ntime.sleep(120)\n''', encoding='utf-8')
    monkeypatch.setenv('SECRET_OCR_TEST', 'sensitive')
    with pytest.raises(subprocess.TimeoutExpired):
        _run_ocr_worker([sys.executable, '-c', script.read_text(encoding='utf-8'), str(marker)], b'', timeout=15)
    pid = int(marker.read_text())
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 10000) == 0
        finally:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.environ.get('TAWZEEF_TEST_REAL_OCR') != '1', reason='Optional local OCR integration')
def test_real_scanned_and_mixed_pdf():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new('RGB', (1200, 600), 'white')
    font = ImageFont.truetype('arial.ttf', 72)
    ImageDraw.Draw(image).text((80, 150), 'PYTHON TEST DOCUMENT', font=font, fill='black')
    scanned = BytesIO()
    image.save(scanned, format='PDF', resolution=150)
    native = PdfReader(BytesIO(make_pdf(['text'])))
    scan = PdfReader(BytesIO(scanned.getvalue()))
    writer = PdfWriter()
    writer.add_page(native.pages[0])
    writer.add_page(scan.pages[0])
    mixed = BytesIO()
    writer.write(mixed)
    pages, ocr_pages = extract_pdf_pages_with_ocr('synthetic.pdf', mixed.getvalue())
    assert ocr_pages == [2]
    assert 'Python' in pages[0]['text']
    assert 'PYTHON' in pages[1]['text'].upper()
    pages, ocr_pages = extract_pdf_pages_with_ocr('synthetic.pdf', scanned.getvalue())
    assert ocr_pages == [1] and 'PYTHON' in pages[0]['text'].upper()
