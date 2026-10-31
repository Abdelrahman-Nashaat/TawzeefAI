"""Private interview availability reservations within a caller's transaction."""


def private_slot_conflict(db, start: str, end: str, interviewer_key: str,
                          *, candidate_id: int | None = None, exclude_slot_id: int | None = None) -> bool:
    """A private slot reserves its interviewer and original candidate until withdrawn."""
    return bool(db.execute('''SELECT 1 FROM interview_slots s
        WHERE s.target_application_id IS NOT NULL AND s.withdrawn=0
          AND s.start_at < ? AND s.end_at > ? AND (? IS NULL OR s.id != ?)
          AND (s.interviewer_key = ? OR (? IS NOT NULL AND s.target_candidate_id = ?)) LIMIT 1''',
        (end, start, exclude_slot_id, exclude_slot_id, interviewer_key, candidate_id, candidate_id)).fetchone())
