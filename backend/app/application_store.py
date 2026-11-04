"""Transactional storage operations for application submission.

The caller authenticates and owns the transaction. PostgreSQL callers must use
named rows and hold the transaction until commit or rollback; SQLite callers
must start BEGIN IMMEDIATE. No endpoint switches databases through this module.
"""


def _dialect(db, dialect):
    if dialect == 'sqlite':
        if not db.in_transaction:
            raise ValueError('Caller must start BEGIN IMMEDIATE before submission')
        return '?'
    if dialect == 'postgres':
        if db.autocommit:
            raise ValueError('Submission requires PostgreSQL autocommit disabled')
        return '%s'
    raise ValueError('Unsupported database dialect')


def create_pending_snapshot(db, application_id, job_id, actor_id, *, dialect='sqlite'):
    placeholder = _dialect(db, dialect)
    job_revision = db.execute(
        f'SELECT revision FROM provisional_job_revisions WHERE job_id={placeholder}', (job_id,)
    ).fetchone()
    assignment_revision = db.execute(
        f'SELECT revision FROM provisional_application_revisions WHERE application_id={placeholder}',
        (application_id,),
    ).fetchone()
    values = (application_id, job_id, job_revision['revision'] if job_revision else 0,
              assignment_revision['revision'] if assignment_revision else 0, 'unreviewed', 'pending', actor_id)
    query = '''INSERT INTO provisional_snapshots
        (application_id,job_id,job_revision,assignment_revision,source_review_status,snapshot_kind,created_by)
        VALUES ({})'''.format(','.join([placeholder] * len(values)))
    if dialect == 'postgres':
        return db.execute(query + ' RETURNING id', values).fetchone()['id']
    return db.execute(query, values).lastrowid


def submit_application(db, *, dialect, candidate_id, job_id, filename, resume_text, extraction_json):
    """Store an application and its notice, audit event, and pending work atomically.

    Candidate/session authorization is checked by the caller in the same write
    transaction. Returns None when the job no longer exists.
    """
    placeholder = _dialect(db, dialect)
    lock = ' FOR SHARE' if dialect == 'postgres' else ''
    if db.execute(f'SELECT id FROM jobs WHERE id={placeholder}{lock}', (job_id,)).fetchone() is None:
        return None
    values = (candidate_id, job_id, filename, resume_text, extraction_json)
    query = 'INSERT INTO applications (candidate_id,job_id,filename,resume_text,extraction_json) VALUES ({})'.format(
        ','.join([placeholder] * len(values)))
    if dialect == 'postgres':
        application_id = db.execute(query + ' RETURNING id', values).fetchone()['id']
    else:
        application_id = db.execute(query, values).lastrowid
    db.execute(f'INSERT INTO hr_notifications(application_id,submitted_job_id) VALUES ({placeholder},{placeholder})',
               (application_id, job_id))
    db.execute(f'INSERT INTO audit_events(application_id,actor_id,action,details) VALUES ({placeholder},{placeholder},{placeholder},{placeholder})',
               (application_id, candidate_id, 'submitted', '{}'))
    create_pending_snapshot(db, application_id, job_id, candidate_id, dialect=dialect)
    return application_id
