import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.knowledge import answer_question


@pytest.fixture
def fixtures(tmp_path: Path):
    (tmp_path / 'policies.json').write_text(json.dumps({
        'documents': [
            {'id': 'pub', 'title': 'Application process', 'audience': 'public',
             'text': 'Applicants can view their application status.'},
            {'id': 'staff', 'title': 'Annual leave', 'audience': 'staff',
             'text': 'Annual leave needs manager approval five working days ahead.'},
            {'id': 'private', 'title': 'Recruiter review procedure', 'audience': 'hr',
             'text': 'Recruiters record their decisions in a private audit ledger.'},
        ], 'hire_patterns': [],
    }), encoding='utf-8')
    (tmp_path / 'jobs.json').write_text(json.dumps({'jobs': [{
        'id': 'job-1', 'title': 'Backend Developer', 'required_skills': ['Python', 'SQL'],
        'preferred_skills': ['testing'], 'responsibilities': ['Build API'],
    }]}), encoding='utf-8')
    return tmp_path


def test_answers_from_permitted_passage_with_source(fixtures):
    result = answer_question('How do I request annual leave?', 'employee', fixture_dir=fixtures)
    assert result.mode == 'extractive'
    assert [s.id for s in result.sources] == ['staff']
    assert 'manager approval' in result.answer


def test_visibility_is_applied_before_retrieval(fixtures):
    result = answer_question('What does recruiter review procedure say?', 'employee', fixture_dir=fixtures)
    assert result.mode == 'abstain'
    assert not result.sources
    result = answer_question('What does recruiter review procedure say?', 'hr', fixture_dir=fixtures)
    assert result.sources[0].id == 'private'
    assert 'private audit ledger' in result.answer


def test_unsupported_empty_question_and_unknown_role_fail_closed(fixtures):
    assert answer_question('', 'hr', fixture_dir=fixtures).mode == 'abstain'
    assert answer_question('Tell me maternity leave entitlement', 'employee', fixture_dir=fixtures).mode == 'abstain'
    with pytest.raises(ValueError):
        answer_question('annual leave', 'admin', fixture_dir=fixtures)


def test_job_description_is_public(fixtures):
    result = answer_question('Backend Developer Python', 'candidate', fixture_dir=fixtures)
    assert any(source.id == 'job-1' and 'SQL' in source.text for source in result.sources)


def test_provider_is_never_called_by_default(fixtures, monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'secret-test-key')
    def fail_if_called(*args, **kwargs):
        raise AssertionError('Unexpected external call')
    monkeypatch.setattr('app.ai_provider._open', fail_if_called)
    assert answer_question('annual leave', 'employee', fixture_dir=fixtures).mode == 'extractive'


def test_provider_sees_only_permitted_sources(fixtures, monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'secret-test-key')
    sent = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({'choices': [{'message': {'content': 'Based on [staff], manager approval is required.'}}]}).encode()
    def fake_urlopen(req):
        sent['body'] = req.data.decode('utf-8')
        return Response()
    monkeypatch.setattr('app.ai_provider._open', fake_urlopen)
    result = answer_question('annual leave', 'employee', fixture_dir=fixtures, enable_groq=True)
    assert result.mode == 'groq'
    assert 'manager approval' in result.answer
    assert 'private audit ledger' not in sent['body']
    assert 'secret-test-key' not in sent['body']
    assert [s.id for s in result.sources] == ['staff']


def test_provider_failure_falls_back_without_exposing_secret(fixtures, monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'secret-test-key')
    def raise_error(*args, **kwargs):
        raise OSError('secret-test-key private exception')
    monkeypatch.setattr('app.ai_provider._open', raise_error)
    result = answer_question('annual leave', 'employee', fixture_dir=fixtures, enable_groq=True)
    assert result.mode == 'extractive'
    assert 'manager approval' in result.answer
    assert 'secret-test-key' not in repr(result)
    assert result.notice


def test_provider_exception_traceback_hides_sensitive_details(monkeypatch):
    from app.ai_provider import ProviderUnavailable, generate_with_groq

    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-secret')
    def fail(*args, **kwargs):
        raise OSError('synthetic-secret PRIVATE DOCUMENT CONTENT')
    monkeypatch.setattr('app.ai_provider._open', fail)
    with pytest.raises(ProviderUnavailable) as error:
        generate_with_groq('question', [{'id': 'example', 'text': 'demo'}])
    formatted = ''.join(traceback.format_exception(error.value))
    assert 'synthetic-secret' not in formatted
    assert 'PRIVATE DOCUMENT CONTENT' not in formatted


def test_missing_api_key_does_not_call_network(fixtures, monkeypatch):
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    monkeypatch.setattr('app.ai_provider._open', lambda *a, **k: pytest.fail('network used'))
    result = answer_question('annual leave', 'employee', fixture_dir=fixtures, enable_groq=True)
    assert result.mode == 'extractive' and result.notice


def test_unsupported_request_is_not_sent_to_provider(fixtures, monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'secret-test-key')
    monkeypatch.setattr('app.ai_provider._open', lambda *a, **k: pytest.fail('network used'))
    result = answer_question('maternity entitlement', 'employee', fixture_dir=fixtures, enable_groq=True)
    assert result.mode == 'abstain'


def test_reference_questions_retrieve_expected_visible_sources():
    base = Path(__file__).resolve().parents[2] / 'data' / 'fixtures'
    cases = json.loads((base / 'policy-questions.json').read_text(encoding='utf-8'))['cases']
    for case in cases:
        result = answer_question(case['question'], case['role'], fixture_dir=base)
        ids = {source.id for source in result.sources}
        if case['expected_outcome'] == 'answer':
            assert set(case['expected_source_ids']) <= ids, case['id']
        else:
            assert result.mode == 'abstain', case['id']
        assert not ids.intersection(case.get('forbidden_source_ids', [])), case['id']


def test_arabic_remote_work_question_returns_authorized_arabic_policy():
    question = 'كام يوم شغل عن بُعد ممكن أطلب في الأسبوع؟'
    result = answer_question(question, 'employee')
    assert result.mode == 'extractive'
    assert [source.id for source in result.sources] == ['pol-remote']
    assert 'يومين' in result.answer and 'موافقة المدير' in result.answer
    denied = answer_question(question, 'candidate')
    assert denied.mode == 'abstain' and not denied.sources


@pytest.mark.parametrize('answer', [
    'Annual leave does not require approval.',
    'Annual leave does not require approval [made-up-source].',
    'Annual leave does not require approval [staff] [private].',
])
def test_generated_answers_without_valid_citations_fall_back(fixtures, monkeypatch, answer):
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-test-key')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({'choices': [{'message': {'content': answer}}]}).encode()
    monkeypatch.setattr('app.ai_provider._open', lambda *a, **k: Response())
    result = answer_question('annual leave', 'employee', fixture_dir=fixtures, enable_groq=True)
    assert result.mode == 'extractive'
    assert 'manager approval' in result.answer
    assert result.notice


def test_valid_citation_does_not_claim_factual_verification(fixtures, monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-test-key')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({'choices': [{'message': {'content': 'No approval is needed [staff].'}}]}).encode()
    monkeypatch.setattr('app.ai_provider._open', lambda *a, **k: Response())
    result = answer_question('annual leave', 'employee', fixture_dir=fixtures, enable_groq=True)
    assert result.mode == 'groq'
    assert 'do not establish factual accuracy' in result.notice


def test_groq_redirect_does_not_forward_authentication(fixtures, monkeypatch):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)

    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{target.server_port}/redirected')
            self.end_headers()

        def log_message(self, *args):
            pass

    source = ThreadingHTTPServer(('127.0.0.1', 0), Redirector)
    servers = [source, target]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        monkeypatch.setenv('GROQ_API_KEY', 'synthetic-secret')
        monkeypatch.setattr('app.ai_provider.GROQ_URL', f'http://127.0.0.1:{source.server_port}/')
        result = answer_question('annual leave', 'employee', fixture_dir=fixtures, enable_groq=True)
        assert result.mode == 'extractive'
        assert received == []
        assert 'synthetic-secret' not in repr(result)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
