"""Explicit desktop OAuth, with tokens stored in Windows Credential Manager.

The authorization command opens a browser for the account owner to complete
Google's consent flow. It does not create a Google Cloud OAuth client.
"""

import argparse
import json
import sys
from pathlib import Path

import keyring
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SERVICE = 'TawzeefAI-Google-OAuth-v1'
SCOPES = {
    'calendar': 'https://www.googleapis.com/auth/calendar.events.owned',
    'gmail': 'https://www.googleapis.com/auth/gmail.send',
}


def _secure_vault():
    if sys.platform != 'win32':
        raise RuntimeError('Google credentials require Windows Credential Manager in this demo')
    from keyring.backends.Windows import WinVaultKeyring

    vault = keyring.get_keyring()
    if not isinstance(vault, WinVaultKeyring):
        raise TypeError('Windows Credential Manager is not available')
    return vault


def _scope(kind: str) -> str:
    if kind not in SCOPES:
        raise ValueError('Unsupported Google service')
    return SCOPES[kind]


def authorize(kind: str, client_file: str | Path) -> None:
    """Request just one service scope and save credentials only on success."""
    scope = _scope(kind)
    vault = _secure_vault()
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_file), scopes=[scope], autogenerate_code_verifier=True,
    )
    credentials = flow.run_local_server(
        host='127.0.0.1', port=0, open_browser=True, access_type='offline', prompt='consent',
        authorization_prompt_message='Complete Google authorization in your browser.',
        success_message='Authorization received. You can close this window.',
    )
    if not credentials.refresh_token:
        raise ValueError('Google did not issue a refresh token; existing credentials were not changed')
    serialized = credentials.to_json()
    try:
        data = json.loads(serialized)
        if not isinstance(data, dict) or set(data.get('scopes', [])) != {scope}:
            raise ValueError('Unexpected authorization scopes')
    except (ValueError, TypeError) as exc:
        raise ValueError('Google authorization returned unexpected scopes') from exc
    vault.set_password(SERVICE, kind, serialized)


def authorized_session(kind: str):
    """Load the selected service credential without retrying write requests."""
    scope = _scope(kind)
    raw = _secure_vault().get_password(SERVICE, kind)
    if not raw or len(raw) > 20000:
        raise RuntimeError('Google authorization is not configured')
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data.get('scopes', [])) != {scope}:
            raise ValueError('Unexpected authorization scopes')
        if data.get('token_uri', 'https://oauth2.googleapis.com/token') != 'https://oauth2.googleapis.com/token':
            raise ValueError('Unexpected token endpoint')
        if data.get('universe_domain', 'googleapis.com') != 'googleapis.com':
            raise ValueError('Unexpected Google domain')
        allowed = {key: data[key] for key in (
            'token', 'refresh_token', 'client_id', 'client_secret', 'expiry', 'scopes',
        ) if key in data}
        credentials = Credentials.from_authorized_user_info(allowed)
        if not credentials.refresh_token:
            raise ValueError('Missing refresh token')
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError('Stored Google authorization is invalid') from exc
    # Avoid transparent retries of Calendar/Gmail write requests after 401.
    return AuthorizedSession(credentials, max_refresh_attempts=0)


def main():
    parser = argparse.ArgumentParser(description='Authorize one Google service for this local demo')
    parser.add_argument('service', choices=tuple(SCOPES))
    parser.add_argument('--client-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        authorize(args.service, args.client_file)
    except Exception as exc:
        raise SystemExit('Google authorization failed; check the client and account setup.') from exc
    print(f'{args.service} authorization stored in Windows Credential Manager.')


if __name__ == '__main__':
    main()
