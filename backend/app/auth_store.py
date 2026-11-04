"""Session persistence shared by the SQLite and future PostgreSQL backends.

The caller owns the transaction and must commit or roll it back. SQLite callers
must start a write transaction before calling ``issue_session``. PostgreSQL callers
must use an explicit transaction: the account row lock serializes the password
and active checks with account updates and session revocations.
"""


class InvalidAccount(Exception):
    """The account changed or was disabled before a session could be issued."""


def _placeholder(dialect):
    if dialect == 'sqlite':
        return '?'
    if dialect == 'postgres':
        return '%s'
    raise ValueError('Unsupported database dialect')


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


def session_user(db, *, dialect, token_hash, now):
    """Look up an unexpired session while enforcing current account status."""
    placeholder = _placeholder(dialect)
    return db.execute(
        f'''SELECT u.id, u.username, u.display_name, u.role
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash={placeholder} AND s.expires_at>{placeholder} AND u.active=1''',
        (token_hash, now),
    ).fetchone()


def reset_password_and_revoke(db, *, dialect, user_id, password_hash):
    """Change a password and revoke sessions in the caller's write transaction."""
    placeholder = _placeholder(dialect)
    if dialect == 'sqlite' and not db.in_transaction:
        raise ValueError('SQLite operation requires an active transaction; caller must use BEGIN IMMEDIATE')
    if dialect == 'postgres' and db.autocommit:
        raise ValueError('PostgreSQL operation requires autocommit disabled')
    # UPDATE takes the same PostgreSQL account row lock as issue_session.
    result = db.execute(f'UPDATE users SET password_hash={placeholder} WHERE id={placeholder}',
                        (password_hash, user_id))
    if result.rowcount != 1:
        raise InvalidAccount
    db.execute(f'DELETE FROM sessions WHERE user_id={placeholder}', (user_id,))
