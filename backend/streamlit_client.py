"""Local-only Streamlit client for the existing TawzeefAI FastAPI server.

Session cookies stay in the current Streamlit session; never cache or share this
client across users. The FastAPI server enforces permissions for every request.
"""

import httpx


class ApiUnavailable(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"API request failed ({status})")


class LocalApi:
    def __init__(self, port: int = 8000, *, transport=None):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Invalid local API port")
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=30,
            follow_redirects=False, trust_env=False, transport=transport,
        )

    def request(self, method: str, path: str, **kwargs):
        if not path.startswith("/api/") or path.startswith("//") or "://" in path:
            raise ValueError("Only local API paths are allowed")
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ApiUnavailable("The local API is unavailable") from None
        if response.is_error or response.is_redirect:
            raise ApiError(response.status_code)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise ApiUnavailable("The local API returned an invalid response") from None

    def close(self):
        self.client.close()
