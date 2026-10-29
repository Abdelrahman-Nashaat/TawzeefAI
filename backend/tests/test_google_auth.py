import importlib
import importlib.util
import json

import pytest

pytest.importorskip('google_auth_oauthlib')
pytest.importorskip('keyring')


class MemoryVault:
    def __init__(self):
        self.values = {}

    def set_password(self, service, name, value):
        self.values[service, name] = value

    def get_password(self, service, name):
        return self.values.get((service, name))


def test_authorization_requests_only_selected_scope_and_stores_after_success(monkeypatch, tmp_path):
    assert importlib.util.find_spec('app.google_auth') is not None
    module = importlib.import_module('app.google_auth')
    vault = MemoryVault()
    monkeypatch.setattr(module, '_secure_vault', lambda: vault)
    observed = {}

    class Credentials:
        refresh_token = 'fake-refresh'
        def to_json(self):
            return json.dumps({'refresh_token': self.refresh_token,
                               'scopes': ['https://www.googleapis.com/auth/calendar.events.owned']})

    class Flow:
        credentials = Credentials()
        def run_local_server(self, **kwargs):
            observed['server'] = kwargs
            return self.credentials

    def create(client_file, scopes, **kwargs):
        observed.update(client_file=client_file, scopes=scopes, options=kwargs)
        return Flow()

    monkeypatch.setattr(module.InstalledAppFlow, 'from_client_secrets_file', create)
    client = tmp_path / 'client.json'
    client.write_text('{}')
    module.authorize('calendar', client)
    assert observed['scopes'] == ['https://www.googleapis.com/auth/calendar.events.owned']
    assert observed['options']['autogenerate_code_verifier'] is True
    assert observed['server']['host'] == '127.0.0.1' and observed['server']['port'] == 0
    assert vault.get_password(module.SERVICE, 'calendar') is not None
    assert vault.get_password(module.SERVICE, 'gmail') is None


def test_authorization_rejects_unexpected_extra_scope_without_overwrite(monkeypatch, tmp_path):
    module = importlib.import_module('app.google_auth')
    vault = MemoryVault()
    vault.set_password(module.SERVICE, 'gmail', 'existing')
    monkeypatch.setattr(module, '_secure_vault', lambda: vault)

    class Credentials:
        refresh_token = 'fake-refresh'
        def to_json(self):
            return json.dumps({'refresh_token': self.refresh_token,
                               'scopes': [module.SCOPES['gmail'], 'https://mail.google.com/']})

    class Flow:
        def run_local_server(self, **kwargs):
            return Credentials()

    monkeypatch.setattr(module.InstalledAppFlow, 'from_client_secrets_file', lambda *args, **kwargs: Flow())
    with pytest.raises(ValueError):
        module.authorize('gmail', tmp_path / 'client.json')
    assert vault.get_password(module.SERVICE, 'gmail') == 'existing'


def test_authorization_without_refresh_token_does_not_overwrite_existing_credentials(monkeypatch, tmp_path):
    assert importlib.util.find_spec('app.google_auth') is not None
    module = importlib.import_module('app.google_auth')
    vault = MemoryVault()
    vault.set_password(module.SERVICE, 'gmail', 'existing')
    monkeypatch.setattr(module, '_secure_vault', lambda: vault)

    class Credentials:
        refresh_token = None

    class Flow:
        credentials = Credentials()
        def run_local_server(self, **kwargs):
            return self.credentials

    monkeypatch.setattr(module.InstalledAppFlow, 'from_client_secrets_file', lambda *args, **kwargs: Flow())
    with pytest.raises(ValueError):
        module.authorize('gmail', tmp_path / 'client.json')
    assert vault.get_password(module.SERVICE, 'gmail') == 'existing'


def test_session_uses_only_expected_scope_and_disables_automatic_retries(monkeypatch):
    module = importlib.import_module('app.google_auth')
    vault = MemoryVault()
    monkeypatch.setattr(module, '_secure_vault', lambda: vault)
    payload = {'client_id': 'client', 'client_secret': 'secret', 'refresh_token': 'refresh',
               'scopes': [module.SCOPES['gmail']]}
    vault.set_password(module.SERVICE, 'gmail', json.dumps(payload))
    calls = []
    monkeypatch.setattr(module, 'AuthorizedSession', lambda credentials, **kwargs: calls.append((credentials, kwargs)))
    module.authorized_session('gmail')
    assert calls[0][1]['max_refresh_attempts'] == 0
    payload['scopes'].append('https://mail.google.com/')
    vault.set_password(module.SERVICE, 'gmail', json.dumps(payload))
    with pytest.raises(RuntimeError):
        module.authorized_session('gmail')
    assert len(calls) == 1
