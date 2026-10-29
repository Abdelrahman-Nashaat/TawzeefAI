"""Conservative local identity masking for an assistive screening workflow.

This rule-based filter is incomplete. It cannot reliably detect unlabeled names,
indirect proxies, or all multilingual PII. A human must verify the masked text
before relying on it as blind; it is not anonymization or a privacy guarantee.
"""

import re
from dataclasses import dataclass

_FIELD = re.compile(
    # CVs commonly format labels as bullets, numbered lists, Markdown headings,
    # or bold text, with the colon inside or outside the bold markers.
    r"^[ \t]*(?:(?:[-*+•▪◦‣]|\d+[.)]|#{1,6})[ \t]+)?(?:\*\*|__)?"
    r"(?:full\s*name|first\s*name|last\s*name|name|email|e-mail|phone|mobile|"
    r"telephone|address|city|location|governorate|nationality|gender|sex|age|"
    r"date\s+of\s+birth|birth\s*date|dob|marital\s+status|university|"
    r"الاسم(?:\s+الكامل)?|اسم|البريد(?:\s+الإلكتروني)?|الايميل|البريد\s+الالكتروني|"
    r"الهاتف|هاتف|التليفون|الموبايل|رقم\s+الهاتف|العنوان|المحافظة|المدينة|الجنسية|"
    r"الجنس|النوع|العمر|السن|تاريخ\s+الميلاد|الحالة\s+الاجتماعية|الجامعة)"
    r"[ \t]*(?:\*\*|__)?[ \t]*[:：-](?:\*\*|__)?",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-zA-Z\u0600-\u06ff]{2,}(?!\w)", re.IGNORECASE)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
# Do not join across newlines, and only mask likely phone numbers rather than
# dates and years. Other formats and national identifiers can still be missed.
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{6,}\d)(?!\w)")


@dataclass(frozen=True)
class BlindText:
    text: str
    masked: bool
    requires_review: bool = True
    notice: str = (
        "Rule-based identity masking is incomplete; a human must review for "
        "remaining identifying information before blind screening."
    )


def redact_resume(text: str) -> BlindText:
    """Redact detected contact/identity lines; never assert complete masking.

    Output is intended for human review before scoring. Entire labelled identity
    lines are removed even if they also contain useful information; this favors
    non-disclosure and requires the reviewer to check for missing evidence.
    """
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    masked = False
    lines = []
    for line in text.splitlines():
        if _FIELD.match(line):
            lines.append('[identity field removed]')
            masked = True
            continue
        clean, email_count = _EMAIL.subn('[email removed]', line)
        clean, url_count = _URL.subn('[link removed]', clean)

        def mask_phone(match):
            nonlocal masked
            digits = sum(char.isdigit() for char in match.group())
            if 8 <= digits <= 15:
                masked = True
                return '[phone removed]'
            return match.group()

        clean = _PHONE.sub(mask_phone, clean)
        masked = masked or bool(email_count or url_count)
        lines.append(clean)
    return BlindText(text='\n'.join(lines), masked=masked)
