"""The optional UI keeps API cookies per session and stays on loopback."""

import httpx
import pytest

from streamlit_client import ApiError, ApiUnavailable, LocalApi


def test_sessions_are_isolated_and_logout_clears_only_its_own_cookie():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get('cookie')))
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'role': 'hr'}, headers={'set-cookie': 'tawzeef_session=first; Path=/; HttpOnly; SameSite=Strict'})
        if request.url.path == '/api/auth/logout':
            return httpx.Response(204, headers={'set-cookie': 'tawzeef_session=; Max-Age=0; Path=/'})
        return httpx.Response(200, json={'ok': True})

    one = LocalApi(8000, transport=httpx.MockTransport(handler))
    two = LocalApi(8000, transport=httpx.MockTransport(handler))
    try:
        one.request('POST', '/api/auth/login', json={'username': 'demo', 'password': 'demo'})
        one.request('GET', '/api/auth/me')
        two.request('GET', '/api/auth/me')
        one.request('POST', '/api/auth/logout')
        one.request('GET', '/api/auth/me')
    finally:
        one.close()
        two.close()
    assert seen[1] == ('http://127.0.0.1:8000/api/auth/me', 'tawzeef_session=first')
    assert seen[2][1] is None and seen[4][1] is None


@pytest.mark.parametrize('path', ['https://example.com/api/x', '//example.com/api/x', '/elsewhere', '/api/https://x'])
def test_client_rejects_non_api_or_remote_paths(path):
    api = LocalApi(8000, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    try:
        with pytest.raises(ValueError):
            api.request('GET', path)
    finally:
        api.close()


@pytest.mark.parametrize('port', [-1, 0, 65536, True, '8000'])
def test_client_rejects_invalid_ports(port):
    with pytest.raises(ValueError):
        LocalApi(port)


def test_client_does_not_follow_redirect_or_expose_transport_error():
    def redirect(_):
        return httpx.Response(302, headers={'location': 'http://example.com/'})

    api = LocalApi(8000, transport=httpx.MockTransport(redirect))
    try:
        with pytest.raises(ApiError) as error:
            api.request('GET', '/api/auth/me')
        assert error.value.status == 302
    finally:
        api.close()

    def fail(request):
        raise httpx.ConnectError('secret from transport', request=request)

    api = LocalApi(8000, transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(ApiUnavailable) as error:
            api.request('GET', '/api/auth/me')
        assert 'secret' not in str(error.value)
    finally:
        api.close()


def test_client_uses_real_api_role_checks_and_revocation(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(tmp_path / 'streamlit.sqlite3', seed_demo=True)
    with TestClient(app) as server:
        def forward(request):
            # Each LocalApi owns its own cookie jar; TestClient must not add its own.
            server.cookies.clear()
            response = server.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        hr = LocalApi(8000, transport=httpx.MockTransport(forward))
        candidate = LocalApi(8000, transport=httpx.MockTransport(forward))
        try:
            hr.request('POST', '/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'})
            candidate.request('POST', '/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'})
            assert hr.request('GET', '/api/dashboard')['applications_total'] == 0
            with pytest.raises(ApiError) as error:
                candidate.request('GET', '/api/dashboard')
            assert error.value.status == 403
            conversation = hr.request('POST', '/api/conversations', json={'title': 'Synthetic'})
            assert candidate.request('GET', '/api/conversations') == []
            with pytest.raises(ApiError):
                candidate.request('GET', f"/api/conversations/{conversation['id']}")
            hr.request('POST', '/api/auth/logout')
            with pytest.raises(ApiError) as error:
                hr.request('GET', '/api/auth/me')
            assert error.value.status == 401
            assert candidate.request('GET', '/api/auth/me')['role'] == 'candidate'
        finally:
            hr.close()
            candidate.close()
