"""Conservative section extraction from a partially redacted résumé.

Text is untrusted. Each item is an exact span of the provided source, not a
verified qualification, inferred date, or judgment of a person's ability.
"""

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
        heading = stripped.rstrip(':：').strip().casefold()
        if heading in headings:
            active = headings[heading]
        elif heading in OTHER_HEADINGS:
            active = None
        elif ':' in body or '：' in body:
            separator = min((body.find(c) for c in ':：' if c in body), default=-1)
            label = body[:separator].strip().casefold()
            if label in headings:
                active = headings[label]
                add(active, body[separator + 1:], position + separator + 1)
            else:
                # Unrecognized labeled fields delimit sections; do not assign
                # personal details or unrelated content to the preceding one.
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
