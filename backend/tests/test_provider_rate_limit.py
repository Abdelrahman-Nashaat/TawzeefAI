"""Rate-limit handling is bounded, credential-scoped and fail-fast."""

import io
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from app import ai_provider


@pytest.fixture(autouse=True)
def clear_rate_limits():
    ai_provider._rate_until.clear()
    yield
    ai_provider._rate_until.clear()


def request(key):
    return urllib.request.Request(ai_provider.GROQ_URL, headers={'Authorization': f'Bearer {key}'})


def test_429_is_scoped_bounded_and_recovers_without_retry_or_sleep(monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(ai_provider, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    calls = []

    def send(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, 'private details', {'Retry-After': '9999'}, io.BytesIO())
        return object()

    monkeypatch.setattr(ai_provider.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=send))
    with pytest.raises(ai_provider.ProviderRateLimited) as error:
        ai_provider._open(request('secret-one'))
    assert 'secret-one' not in str(error.value) and 'private details' not in str(error.value)
    with pytest.raises(ai_provider.ProviderRateLimited):
        ai_provider._open(request('secret-one'))
    assert len(calls) == 1
    ai_provider._open(request('secret-two'))
    assert len(calls) == 2
    assert all('secret' not in key for key in ai_provider._rate_until)
    clock.value += 61
    ai_provider._open(request('secret-one'))
    assert len(calls) == 3


@pytest.mark.parametrize('header,expected', [('0', 10), ('nan', 10), ('invalid', 10), ('0.1', 1), ('3', 3)])
def test_retry_after_is_bounded(header, expected):
    assert ai_provider._retry_delay(header) == expected


def test_other_http_errors_do_not_start_cooldown(monkeypatch):
    calls = []
    def send(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 400, 'private', {}, io.BytesIO())
    monkeypatch.setattr(ai_provider.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=send))
    for _ in range(2):
        with pytest.raises(urllib.error.HTTPError):
            ai_provider._open(request('secret'))
    assert len(calls) == 2 and not ai_provider._rate_until
