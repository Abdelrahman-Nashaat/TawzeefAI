import sys

import pytest

from app.main import create_app


def test_google_is_disabled_without_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv('TAWZEEF_ENABLE_GOOGLE', raising=False)
    app = create_app(tmp_path / 'test.sqlite3')
    assert getattr(app.state, 'google_preflight', None) is None


def test_opt_in_wires_google_providers_only_on_windows(monkeypatch, tmp_path):
    monkeypatch.setenv('TAWZEEF_ENABLE_GOOGLE', '1')
    if sys.platform != 'win32':
        with pytest.raises(RuntimeError, match='Windows Credential Manager'):
            create_app(tmp_path / 'test.sqlite3')
        return
    app = create_app(tmp_path / 'test.sqlite3')
    assert callable(app.state.google_preflight)
    assert callable(app.state.google_calendar_removal_preflight)
