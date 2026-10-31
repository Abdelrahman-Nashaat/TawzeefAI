"""Local, extractive candidates from explicitly labelled résumé fields.

The caller must authorize and redact the source first. Values are not verified
facts, and the redaction itself may be incomplete. No inference, network call,
persistence, or decision is performed here.
"""

import re
import unicodedata
from hashlib import sha256

MAX_SOURCE_CHARS = 200_000
MAX_RECORDS = 100
MAX_FIELD_CHARS = 1000

_HEADINGS = {
    'experience': ('experience', 'work experience', 'professional experience', 'employment history',
                   'الخبرة', 'الخبرات', 'الخبرة العملية', 'الخبرات العملية', 'خبرة العمل'),
    'education': ('education', 'academic background', 'educational background',
                  'التعليم', 'المؤهل العلمي', 'المؤهلات العلمية'),
    'projects': ('projects', 'personal projects', 'project experience', 'المشاريع', 'مشاريع', 'المشروعات'),
}

_FIELDS = {
    'experience': {
        'role': ('role', 'job title', 'position', 'المسمى الوظيفي', 'الدور', 'الوظيفة'),
        'organization': ('company', 'employer', 'organization', 'الشركة', 'جهة العمل', 'المؤسسة'),
        'period': ('dates', 'date', 'period', 'الفترة', 'المدة', 'التواريخ'),
    },
    'education': {
        'qualification': ('degree', 'qualification', 'المؤهل', 'الدرجة العلمية', 'الشهادة'),
        'institution': ('institution', 'university', 'school', 'الجامعة', 'المؤسسة التعليمية', 'المدرسة'),
        'period': ('dates', 'date', 'period', 'year', 'الفترة', 'المدة', 'السنة', 'التواريخ'),
    },
    'projects': {
        'name': ('project', 'project name', 'name', 'المشروع', 'اسم المشروع'),
        'role': ('role', 'الدور'),
        'technologies': ('technologies', 'technology', 'tools', 'التقنيات', 'الأدوات'),
        'period': ('dates', 'date', 'period', 'الفترة', 'المدة', 'التواريخ'),
    },
}

# Known non-target section headings end the current section. An unknown heading
# also ends it instead of attributing later fields to the preceding section.
_OTHER_HEADINGS = frozenset(('skills', 'technical skills', 'languages', 'summary', 'profile',
                             'contact', 'personal details', 'references', 'certifications',
                             'المهارات', 'اللغات', 'الملخص', 'البيانات الشخصية', 'المراجع',
                             'الشهادات', 'معلومات التواصل'))
_PREFIX = re.compile(r'^\s*(?:(?:[-+*•▪◦‣]|\d+[.)])\s+|#{1,6}\s+)?')


def resume_fields_for_display(blind_source: str) -> dict:
    """Report a bounded, non-partial result for an authorized display."""
    if not isinstance(blind_source, str):
        raise TypeError('blind_source must be a string')
    if len(blind_source) > MAX_SOURCE_CHARS:
        return {
            'status': 'limit_exceeded',
            'requires_human_review': True,
            'notice': 'The redacted resume exceeds the field extraction limit; review the original manually.',
        }
    return {'status': 'current', **extract_resume_fields(blind_source)}


def _normalize(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().strip().split())


def extract_resume_fields(blind_source: str) -> dict:
    """Return explicitly labelled field candidates with exact source offsets.

    Offsets count Python Unicode code points in the supplied source. Repeated
    labels or a blank line start a new record; grouping is tentative and may be
    wrong. Missing information means unknown, not a negative qualification.
    """
    if not isinstance(blind_source, str):
        raise TypeError('blind_source must be a string')
    if len(blind_source) > MAX_SOURCE_CHARS:
        raise ValueError('Resume text exceeds the field extraction limit')
    headings = {_normalize(name): section for section, names in _HEADINGS.items() for name in names}
    other_headings = {_normalize(item) for item in _OTHER_HEADINGS}
    field_names = {section: {_normalize(label): field for field, labels in fields.items() for label in labels}
                   for section, fields in _FIELDS.items()}
    sections = {section: {'status': 'unknown', 'records': [], 'unclassified_lines': False, 'truncated': False}
                for section in _HEADINGS}
    active = None
    current = None
    position = 0

    def new_record(section: str) -> dict | None:
        target = sections[section]
        if len(target['records']) >= MAX_RECORDS:
            target['truncated'] = True
            return None
        record = {'fields': {name: None for name in _FIELDS[section]}}
        target['records'].append(record)
        return record

    for line in blind_source.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        prefix = _PREFIX.match(body).end()
        stripped = body[prefix:].strip()
        if not stripped:
            current = None
            position += len(line)
            continue
        heading_text = stripped.rstrip(':：').strip()
        heading = _normalize(heading_text)
        if heading in headings:
            active = headings[heading]
            sections[active]['status'] = 'recognized'
            current = None
        elif heading in other_headings:
            active = None
            current = None
        elif active is not None:
            match = re.match(r'([^:：]{1,80})[:：](.*)$', body[prefix:])
            if not match:
                # An unrecognized line may be a heading, so stop attribution
                # until another recognized heading appears.
                sections[active]['unclassified_lines'] = True
                active = None
                current = None
            else:
                label = _normalize(match.group(1).strip())
                field = field_names[active].get(label)
                if field is None:
                    sections[active]['unclassified_lines'] = True
                    active = None
                    current = None
                else:
                    raw = match.group(2)
                    value = raw.strip()
                    if not value or value == '[identity field removed]':
                        sections[active]['unclassified_lines'] = True
                    elif len(value) > MAX_FIELD_CHARS:
                        sections[active]['truncated'] = True
                    else:
                        if current is None or current['fields'][field] is not None:
                            current = new_record(active)
                        if current is not None:
                            start = position + prefix + match.start(2) + (len(raw) - len(raw.lstrip()))
                            current['fields'][field] = {'value': value, 'start': start, 'end': start + len(value)}
        position += len(line)
    return {
        'source_kind': 'partially_redacted_resume',
        'source_sha256': sha256(blind_source.encode('utf-8')).hexdigest(),
        'offset_unit': 'unicode_code_points',
        'sections': sections,
        'requires_human_review': True,
        'notice': ('Only explicitly labelled values are extracted. Record grouping is tentative; '
                   'values are unverified and missing fields are unknown. Redaction may be incomplete.'),
    }
