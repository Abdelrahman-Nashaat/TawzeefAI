"""Session persistence shared by the SQLite and future PostgreSQL backends.

The caller owns the transaction and must commit or roll it back. SQLite callers
must start a write transaction before calling ``issue_session``. PostgreSQL callers
must use an explicit transaction: the account row lock serializes the password
and active checks with account updates and session revocations.
"""


class InvalidAccount(Exception):
    """The account changed or was disabled before a session could be issued."""


def issue_session(db, *, dialect, user_id, expected_password_hash, token_hash, now, expires_at):
    if dialect == 'sqlite':
        if not db.in_transaction:
            raise ValueError('SQLite session issuance requires an active transaction; caller must use BEGIN IMMEDIATE')
        placeholder, lock = '?', ''
    elif dialect == 'postgres':
        if db.autocommit:
            raise ValueError('PostgreSQL session issuance requires autocommit disabled')
        placeholder, lock = '%s', ' FOR UPDATE'
    else:
        raise ValueError('Unsupported database dialect')
    account = db.execute(
        f'SELECT active,password_hash FROM users WHERE id = {placeholder}{lock}', (user_id,)
    ).fetchone()
    if not account or not account['active'] or account['password_hash'] != expected_password_hash:
        raise InvalidAccount
    db.execute(f'DELETE FROM sessions WHERE expires_at <= {placeholder}', (now,))
    db.execute(
        f'INSERT INTO sessions (token_hash,user_id,expires_at) VALUES ({placeholder},{placeholder},{placeholder})',
        (token_hash, user_id, expires_at),
    )
