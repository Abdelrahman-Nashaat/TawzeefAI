"""Local lexical evidence retrieval with explicit role-based filtering.

The default response is extractive and not a generative AI answer. The optional
external model can be wrong or manipulated and must not make hiring decisions.
The caller authenticates the user and supplies the server-derived role; this
module must not receive role claims straight from an untrusted request.
"""

import json
import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.ai_provider import ProviderUnavailable, generate_with_groq
from app.db import connect
from app.document_text import OCR_NOTICE
from app.knowledge_documents import fetch_authorized_documents
from app.knowledge_documents import include_sample_policies as are_samples_enabled

DEFAULT_FIXTURES = Path(__file__).resolve().parents[2] / 'data' / 'fixtures'
ROLE_AUDIENCES = {
    'candidate': {'public'},
    'employee': {'public', 'staff'},
    'hr': {'public', 'staff', 'hr'},
}
STOPWORDS = {
    'a', 'an', 'and', 'are', 'at', 'be', 'by', 'can', 'could', 'do', 'does',
    'for', 'from', 'how', 'i', 'in', 'is', 'it', 'may', 'me', 'my', 'of', 'on',
    'or', 'our', 'should', 'the', 'their', 'there', 'to', 'we', 'what', 'when',
    'where', 'which', 'who', 'why', 'will', 'with', 'would', 'you', 'your',
    'policy', 'policies', 'say', 'tell', 'please', 'much', 'many', 'far', 'advance',
    'about', 'itself', 'company', 'fictional', 'eligible', 'request',
    'كام', 'كم', 'ممكن', 'ماذا', 'متى', 'كيف', 'اين', 'هل', 'يمكن',
    'من', 'عن', 'الى', 'على', 'في', 'ما', 'هو', 'هي', 'هذا', 'هذه',
    'information', 'required', 'sample', 'reported',
}
ARABIC_EQUIVALENTS = {'يومين': 'يوم', 'اطلب': 'طلب', 'يطلب': 'طلب'}
DOCUMENT_CHUNK_SIZE = 1600
DOCUMENT_CHUNK_OVERLAP = 200


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    text: str
    page: int | None = None
    section: str | None = None
    extraction: str | None = None
    extraction_notice: str | None = None


@dataclass(frozen=True)
class KnowledgeAnswer:
    answer: str
    sources: list[Source]
    mode: str
    notice: str | None = None


def _token_sequence(text: str) -> list[str]:
    normalized = unicodedata.normalize('NFKD', text.casefold())
    normalized = ''.join(char for char in normalized if not unicodedata.combining(char))
    words = re.findall(r'[^\W_]+', normalized, flags=re.UNICODE)
    result = []
    for word in words:
        if word in STOPWORDS or len(word) < 3:
            continue
        # Limited normalization; this is not semantic search or a full stemmer.
        if re.fullmatch(r'[\u0621-\u064a]+', word):
            word = word.replace('ة', 'ه').replace('ى', 'ي')
            if len(word) > 5 and word.startswith(('وال', 'بال', 'كال', 'فال')):
                word = word[3:]
            elif len(word) > 4 and word.startswith('ال'):
                word = word[2:]
        if len(word) > 4 and word.endswith('s') and not word.endswith(('ss', 'us')):
            word = word[:-1]
        word = ARABIC_EQUIVALENTS.get(word, word)
        result.append(word)
    return result


def _tokens(text: str) -> set[str]:
    return set(_token_sequence(text))


def is_context_followup(question: str) -> bool:
    """Recognize a small set of reference-only questions, not arbitrary topics."""
    normalized = unicodedata.normalize('NFKD', question.casefold())
    normalized = ''.join(char for char in normalized if not unicodedata.combining(char))
    words = ' '.join(re.findall(r'[^\W_]+', normalized, flags=re.UNICODE))
    return words in {
        'what about it', 'what about that', 'what about this', 'what about them',
        'and what about it', 'and what about that', 'tell me more about it',
        'tell me more about that', 'how long is it', 'how long does it last',
        'وماذا عنها', 'ماذا عنها', 'وماذا عنه', 'ماذا عنه',
        'كم مدتها', 'كم مدته', 'ما مدتها', 'ما مدته',
    }


def _rank_lexical(terms: set[str], documents: list[Source]) -> list[tuple[float, Source]]:
    """Rank authorized passages with BM25, retaining a conservative match gate.

    The gate requires at least two matching words and 60% query coverage. It
    reduces unrelated single-word matches but does not prove answer support.
    """
    if len(terms) < 2 or not documents:
        return []
    counters = [Counter(_token_sequence(source.title + ' ' + source.title + ' ' + source.text))
                for source in documents]
    lengths = [sum(counter.values()) for counter in counters]
    average = sum(lengths) / len(lengths) or 1.0
    frequency = Counter(term for counter in counters for term in terms if term in counter)
    ranked = []
    for source, counter, length in zip(documents, counters, lengths, strict=True):
        matches = terms.intersection(counter)
        if len(matches) < 2 or len(matches) / len(terms) < 0.6:
            continue
        score = 0.0
        for term in matches:
            inverse_frequency = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            count = counter[term]
            score += inverse_frequency * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
        ranked.append((score, source))
    ranked.sort(key=lambda item: (-item[0], item[1].id))
    return ranked


def _has_valid_citations(answer: str, sources: list[Source]) -> bool:
    """Check citation identifiers only; this does not establish factual support."""
    matches = re.findall(r'\[([^\[\]\n]{1,128})\]', answer)
    remainder = re.sub(r'\[([^\[\]\n]{1,128})\]', '', answer)
    return bool(matches) and '[' not in remainder and ']' not in remainder and set(matches) <= {
        source.id for source in sources
    }


def _documents(fixture_dir: Path, role: str, *, include_samples: bool = True,
               include_fixture_jobs: bool = True) -> list[Source]:
    allowed = ROLE_AUDIENCES[role]
    sources = []
    if include_samples:
        policies = json.loads((fixture_dir / 'policies.json').read_text(encoding='utf-8'))
        for doc in [*policies.get('documents', []), *policies.get('hire_patterns', [])]:
            if doc.get('audience') in allowed:
                sources.append(Source(id=doc['id'], title=doc['title'], text=doc['text']))
    if include_fixture_jobs:
        jobs = json.loads((fixture_dir / 'jobs.json').read_text(encoding='utf-8'))
        for job in jobs.get('jobs', []):
            fields = [job['title'], job.get('location', '')]
            for field in ('required_skills', 'preferred_skills', 'responsibilities'):
                fields.extend(job.get(field, []))
            sources.append(Source(id=job['id'], title=job['title'], text='; '.join(fields)))
    return sources


def _document_passages(document: dict) -> list[Source]:
    """Split a saved document into stable, overlapping bounded passages.

    IDs are deterministic for an unchanged document and retain its database ID.
    Whitespace boundaries are preferred; long unbroken runs remain bounded.
    """
    stored_pages = document.get('pages_json')
    stored_sections = document.get('sections_json')
    ocr_pages = None
    if document.get('ocr_pages_json') is not None:
        try:
            ocr_pages = json.loads(document['ocr_pages_json'])
            if (not isinstance(ocr_pages, list) or any(type(p) is not int or p < 1 for p in ocr_pages)
                    or len(set(ocr_pages)) != len(ocr_pages)):
                return []
        except (TypeError, ValueError):
            return []
    if stored_pages is not None and stored_sections is not None:
        return []
    if stored_pages is not None:
        try:
            pages = json.loads(stored_pages)
            if (not isinstance(pages, list) or not pages or
                any(not isinstance(p, dict) or type(p.get('page')) is not int or
                    p['page'] != index or not isinstance(p.get('text'), str)
                    for index, p in enumerate(pages, 1)) or
                '\n'.join(p['text'] for p in pages if p['text']) != document['text']):
                return []
            if ocr_pages is not None and any(p > len(pages) for p in ocr_pages):
                return []
        except (TypeError, ValueError):
            return []
    elif stored_sections is not None:
        try:
            sections = json.loads(stored_sections)
            if (not isinstance(sections, list) or not sections or len(sections) > 1000 or
                any(not isinstance(s, dict) or set(s) != {'section', 'text'} or
                    not isinstance(s['text'], str) or not s['text'] or
                    (s['section'] is not None and (not isinstance(s['section'], str) or
                     not 1 <= len(s['section']) <= 200 or
                     not (s['text'] == s['section'] or s['text'].startswith(s['section'] + '\n'))))
                    for s in sections) or
                '\n'.join(s['text'] for s in sections) != document['text']):
                return []
            pages = [{'page': None, **s} for s in sections]
        except (TypeError, ValueError):
            return []
    else:
        pages = [{'page': None, 'text': document['text']}]
    passages = []
    for page in pages:
        text = page['text']
        start = 0
        while start < len(text):
            end = min(start + DOCUMENT_CHUNK_SIZE, len(text))
            if end < len(text):
                boundary = text.rfind(' ', start + DOCUMENT_CHUNK_SIZE // 2, end)
                if boundary > start:
                    end = boundary
            body = text[start:end].strip()
            if body:
                passages.append(Source(
                    id=f"db-doc-{document['id']}-p{len(passages) + 1}",
                    title=document['title'], text=body, page=page['page'], section=page.get('section'),
                    extraction=('ocr' if page['page'] in ocr_pages else 'native') if ocr_pages is not None else None,
                    extraction_notice=OCR_NOTICE if ocr_pages and page['page'] in ocr_pages else None,
                ))
            if end == len(text):
                break
            start = max(start + 1, end - DOCUMENT_CHUNK_OVERLAP)
    return passages


def answer_question(
    question: str,
    role: str,
    *,
    fixture_dir: Path | None = None,
    enable_groq: bool = False,
    database_path: Path | str | None = None,
    authorize_sources=None,
    previous_question: str = '',
) -> KnowledgeAnswer:
    """Find authorized evidence and optionally ask Groq to summarize it.

    Unmatched/empty questions abstain; Groq failure returns extractive evidence.
    Optional hybrid candidates are checked against currently authorized source
    content before use. Valid citation IDs do not prove factual support.
    """
    if role not in ROLE_AUDIENCES:
        raise ValueError('Unsupported role')
    if not isinstance(question, str):
        raise TypeError('question must be a string')
    if len(question) > 2000:
        raise ValueError('Question exceeds 2000 characters')
    if not isinstance(previous_question, str) or len(previous_question) > 500:
        raise ValueError('Previous question exceeds context limit')
    if previous_question and len(question) + len(previous_question) + 1 > 2000:
        raise ValueError('Combined retrieval question exceeds 2000 characters')
    if not is_context_followup(question):
        previous_question = ''
    # Reference-only follow-ups add no new topic words for retrieval. Retain
    # the original question separately for generation; otherwise Arabic or
    # English pronouns can dilute the lexical match and bury the prior topic.
    retrieval_question = previous_question or question
    terms = _tokens(retrieval_question)
    if not terms:
        return KnowledgeAnswer('No answer was found in the accessible sample documents.', [], 'abstain')

    hybrid_rows = []
    hybrid_notice = None
    if database_path is not None and os.environ.get('TAWZEEF_USE_HYBRID_ANSWERS') == '1':
        try:
            from app.hybrid_api import retrieve_hybrid

            result = retrieve_hybrid(database_path, role, retrieval_question)
            if not isinstance(result, dict) or not isinstance(result.get('sources'), list):
                raise TypeError('Invalid hybrid response')
            hybrid_rows = result['sources']
            hybrid_notice = 'Experimental hybrid retrieval; verify the sources.'
        except Exception:  # noqa: BLE001 - a local search failure falls back to lexical retrieval.
            hybrid_notice = 'Hybrid retrieval unavailable; using lexical retrieval.'

    # Load sources after the optional search, so a changed or revoked document
    # cannot be passed to the provider just because it appeared in an old index.
    documents = _documents(
        fixture_dir or DEFAULT_FIXTURES, role,
        include_samples=are_samples_enabled(database_path) if database_path is not None else True,
        include_fixture_jobs=database_path is None,
    )
    if database_path is not None:
        existing_ids = {source.id for source in documents}
        with connect(database_path) as db:
            jobs = db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        for job in jobs:
            fields = [job['title'], job['location']]
            for field in ('required_skills', 'preferred_skills', 'responsibilities'):
                fields.extend(json.loads(job[field]))
            source = Source(id=f"job-db-{job['id']}", title=job['title'], text='; '.join(fields))
            if source.id in existing_ids:
                raise ValueError('Duplicate source identifier')
            documents.append(source)
            existing_ids.add(source.id)
        for document in fetch_authorized_documents(database_path, role):
            for source in _document_passages(document):
                if source.id in existing_ids:
                    raise ValueError('Duplicate source identifier')
                documents.append(source)
                existing_ids.add(source.id)

    by_id = {source.id: source for source in documents}
    hybrid = []
    for row in hybrid_rows[:10]:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            continue
        source = by_id.get(row.get('id'))
        if source is not None and all(row.get(key) == getattr(source, key) for key in ('title', 'text', 'page', 'section')):
            hybrid.append(source)
    ranked = _rank_lexical(terms, documents)
    # Fuse the ranked candidate lists, retaining lexical jobs and sample
    # collections that the experimental vector index does not yet cover.
    lexical = [source for _, source in ranked]
    combined = []
    for index in range(max(len(hybrid), len(lexical))):
        if index < len(hybrid):
            combined.append(hybrid[index])
        if index < len(lexical):
            combined.append(lexical[index])
    # First select the highest-ranked passage from each document, then fill
    # remaining slots with next-best passages. One long source cannot crowd
    # out every other relevant document.
    distinct, repeats, seen = [], [], set()
    seen_ids = set()
    for source in combined:
        if source.id in seen_ids:
            continue
        seen_ids.add(source.id)
        match = re.fullmatch(r'(db-doc-[0-9]+)-p[0-9]+', source.id)
        group = match.group(1) if match else source.id
        if group in seen:
            repeats.append(source)
        else:
            seen.add(group)
            distinct.append(source)
    sources = (distinct + repeats)[:3]
    if not sources:
        return KnowledgeAnswer('No answer was found in the accessible sample documents.', [], 'abstain', hybrid_notice)

    extractive = '\n\n'.join(f'[{s.id}] {s.text}' for s in sources)
    if enable_groq:
        if authorize_sources is not None:
            authorize_sources(sources)
        try:
            passages = [
                {'id': s.id, 'title': s.title, 'text': s.text[:4000]} for s in sources
            ]
            if previous_question:
                answer = generate_with_groq(question, passages, previous_question=previous_question)
            else:
                answer = generate_with_groq(question, passages)
            if not _has_valid_citations(answer, sources):
                return KnowledgeAnswer(
                    extractive, sources, 'extractive',
                    ' '.join(filter(None, [hybrid_notice, 'AI citations could not be verified; showing source excerpts.'])),
                )
            return KnowledgeAnswer(
                answer, sources, 'groq',
                ' '.join(filter(None, [hybrid_notice, 'AI-generated; valid citation IDs do not establish factual accuracy. Verify the sources.'])),
            )
        except ProviderUnavailable:
            return KnowledgeAnswer(extractive, sources, 'extractive', ' '.join(filter(None, [hybrid_notice, 'AI service unavailable; showing source excerpts.'])))
    return KnowledgeAnswer(extractive, sources, 'extractive', hybrid_notice)
