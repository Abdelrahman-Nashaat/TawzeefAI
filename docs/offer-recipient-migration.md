# Offer recipient migration

New offers record the intended candidate when HR presents them. Candidate offer
access and responses, and the HR hire decision, require that recipient to match
the application's current owner. HR can inspect the recipient ID on an offer.
Changing an existing, non-null recipient is rejected by the database.

Existing offers did not record their recipient. Startup adds a nullable column
without guessing from the application's present owner. Such offers remain in the
database and visible to HR, but candidates cannot read or respond to them, and
an unassigned accepted offer cannot authorize hiring. Pending offers can be
withdrawn through the HR interface.

There is no automatic recovery for unattributed legacy offers. For any recovery,
the administrator must independently establish the original recipient and the
validity of any acceptance from reliable records, preserve a backup, and record
the evidence and intervention in the organization's audit process. If that
evidence is unavailable, leave the recipient unset and resolve the application
with HR instead of assuming that its present owner was the original recipient.
Assigning a verified recipient requires an explicit database operation and is not
available through the application UI. A database administrator can identify
unattributed records with `SELECT id, application_id, status FROM job_offers
WHERE recipient_id IS NULL`. For each independently verified record, assign the
verified user ID only where `recipient_id IS NULL`, confirm exactly one row was
updated, and record the offer ID, actor, time, and evidence reference together
in a single database transaction. Enable foreign keys. An assigned recipient
cannot subsequently be changed by this migration mechanism. Verify the owner
and current status separately before resuming any application. Existing data are
not deleted.

This binds offer records only. Directly changing an application's owner can also
affect other application data and statuses; there is no supported ownership
reassignment feature or automatic reassignment of associated records.
