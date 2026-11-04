# PostgreSQL session issuance foundation

The current application and its login endpoint still use SQLite. The shared
session issuance operation in `app/auth_store.py` is also exercised against
PostgreSQL as a migration building block. It does not switch the application
database, provide PostgreSQL-backed login, or make a partial database safe to
deploy.

Callers own the connection and transaction. SQLite callers must start an
immediate write transaction before calling the operation. PostgreSQL callers
must use a connection with autocommit disabled and named rows, and commit or
roll back promptly. The operation locks the user account row before
checking its current password hash and active state, removing expired sessions,
and inserting the new session. Future PostgreSQL password resets, account
deactivation, role changes and session revocation must update or lock that same
account row and revoke sessions in the same transaction. A service that skips
that ordering would invalidate the race protection.

The SQLite transaction check can verify that a transaction is active, but
cannot distinguish `BEGIN IMMEDIATE` from a deferred transaction. The caller
is responsible for starting the required immediate transaction. The current
SQLite login path does so.

The operation expects compatible `users` and `sessions` tables; it does not
create a production schema. The tests create and remove a randomly named schema
inside an explicitly configured disposable database. Set
`TAWZEEF_TEST_POSTGRES_DSN` privately and run
`pytest -q tests/test_postgres_session_store.py tests/test_auth.py` to exercise
both implementations. With no test DSN, PostgreSQL tests are skipped.

The shared store also implements current-session lookup and password reset with
session revocation. The SQLite authentication and administrator endpoints use
those operations already. The PostgreSQL reset updates the account row before
deleting its sessions in the caller's transaction, so it follows the same lock
order as session issuance. Test coverage includes expiry and account status,
transaction rollback, and both orderings of the password reset and issuance
race. PostgreSQL support for the remaining endpoints, account administration,
and a complete application schema is still required before switching databases.
