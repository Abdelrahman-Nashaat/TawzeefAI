"""Conservative section extraction from a partially redacted résumé.

Text is untrusted. Each item is an exact span of the provided source, not a
verified qualification, inferred date, or judgment of a person's ability.
"""

import re
from hashlib import sha256

SECTION_HEADINGS = {
    'skills': ('skills', 'technical skills', 'مهارات', 'المهارات', 'المهارات التقنية'),
    'experience': ('experience', 'work experience', 'professional experience', 'الخبرة', 'الخبرات', 'خبرة العمل'),
    'education': ('education', 'academic background', 'التعليم', 'المؤهل العلمي', 'المؤهلات العلمية'),
    'projects': ('projects', 'personal projects', 'المشاريع', 'مشاريع'),
    'languages': ('languages', 'اللغات'),
}
OTHER_HEADINGS = frozenset(('summary', 'profile', 'contact', 'personal details', 'references',
                            'الملخص', 'الملف الشخصي', 'البيانات الشخصية', 'معلومات التواصل', 'المراجع'))
MAX_ITEMS = 100
MAX_ITEM_CHARS = 1000
_MARKDOWN_HEADING = re.compile(r'^\s*#{1,6}[ \t]+')
_LIST_PREFIX = re.compile(r'^\s*(?:[-+*•▪◦‣]|\d+[.)])[ \t]+')


def _label(raw: str) -> str:
    """Normalize formatting around a heading, never its evidence text."""
    text = _MARKDOWN_HEADING.sub('', raw, count=1)
    text = _LIST_PREFIX.sub('', text, count=1).strip()
    for marker in ('**', '__'):
        if text.startswith(marker):
            text = text[len(marker):].lstrip()
        if text.endswith(marker):
            text = text[:-len(marker)].rstrip()
    return text.casefold()


def structure_resume(source: str) -> dict:
    """Identify explicitly headed sections only; offsets use Unicode code points.

    The caller chooses and authorizes the exact source. No information is
    inferred when a section is missing, empty, or unrecognized.
    """
    headings = {label.casefold(): key for key, labels in SECTION_HEADINGS.items() for label in labels}
    sections = {key: {'status': 'unknown', 'items': [], 'truncated': False} for key in SECTION_HEADINGS}
    active = None
    position = 0

    def add(key: str, raw: str, offset: int) -> None:
        leading = len(raw) - len(raw.lstrip())
        content = raw.strip()
        if not content or content == '[identity field removed]':
            return
        section = sections[key]
        if len(section['items']) >= MAX_ITEMS:
            section['truncated'] = True
            return
        if len(content) > MAX_ITEM_CHARS:
            content = content[:MAX_ITEM_CHARS]
            section['truncated'] = True
        start = offset + leading
        section['items'].append({'text': content, 'start': start, 'end': start + len(content)})
        section['status'] = 'mentioned'

    for line in source.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        stripped = body.strip()
        heading = _label(stripped.rstrip(':：').strip())
        if heading in headings:
            active = headings[heading]
        elif heading in OTHER_HEADINGS:
            active = None
        elif ':' in body or '：' in body:
            separator = min((body.find(c) for c in ':：' if c in body), default=-1)
            raw_label = body[:separator].strip()
            label = _label(raw_label)
            if label in headings:
                active = headings[label]
                start = separator + 1
                # In **Skills:** value the marker closes the label after ':';
                # keep the extracted value an exact source span.
                label_with_markup = _LIST_PREFIX.sub('', _MARKDOWN_HEADING.sub('', raw_label, count=1), count=1)
                for marker in ('**', '__'):
                    if (label_with_markup.startswith(marker) and not label_with_markup.endswith(marker)
                            and body[start:].startswith(marker)):
                        start += len(marker)
                        break
                add(active, body[start:], position + start)
            else:
                # Unrecognized labeled fields delimit sections; do not assign
                # personal details or unrelated content to the preceding one.
                active = None
        elif _MARKDOWN_HEADING.match(body):
            # An unknown Markdown heading begins a different section.
            active = None
        elif active is not None:
            add(active, body, position)
        position += len(line)
    return {
        'source_kind': 'partially_redacted_resume',
        'source_sha256': sha256(source.encode('utf-8')).hexdigest(),
        'offset_unit': 'unicode_code_points',
        'sections': sections,
        'requires_human_review': True,
        'notice': ('Extractive section identification only; mentions are unverified, missing sections are unknown, '
                   'redaction is incomplete, and the extracted document may omit or misorder content.'),
    }
