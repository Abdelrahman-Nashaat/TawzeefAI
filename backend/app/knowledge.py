"""Local lexical evidence retrieval with explicit role-based filtering.

The default response is extractive and not a generative AI answer. The optional
external model can be wrong or manipulated and must not make hiring decisions.
The caller authenticates the user and supplies the server-derived role; this
module must not receive role claims straight from an untrusted request.
"""

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.ai_provider import ProviderUnavailable, generate_with_groq
from app.db import connect
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
    if stored_pages is not None:
        try:
            pages = json.loads(stored_pages)
            if (not isinstance(pages, list) or not pages or
                any(not isinstance(p, dict) or type(p.get('page')) is not int or
                    p['page'] != index or not isinstance(p.get('text'), str)
                    for index, p in enumerate(pages, 1)) or
                '\n'.join(p['text'] for p in pages if p['text']) != document['text']):
                return []
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
                    title=document['title'], text=body, page=page['page'],
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
) -> KnowledgeAnswer:
    """Find authorized evidence and optionally ask Groq to summarize it.

    Unmatched/empty questions abstain; Groq failure returns extractive evidence.
    Retrieval is lexical and includes only limited Arabic normalization. Valid
    citation IDs do not prove that generated claims are factually grounded.
    """
    if role not in ROLE_AUDIENCES:
        raise ValueError('Unsupported role')
    if not isinstance(question, str):
        raise TypeError('question must be a string')
    if len(question) > 2000:
        raise ValueError('Question exceeds 2000 characters')
    terms = _tokens(question)
    if not terms:
        return KnowledgeAnswer('No answer was found in the accessible sample documents.', [], 'abstain')

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

    ranked = _rank_lexical(terms, documents)
    # First select the highest-ranked passage from each document, then fill
    # remaining slots with next-best passages. One long source cannot crowd
    # out every other relevant document.
    distinct, repeats, seen = [], [], set()
    for _, source in ranked:
        match = re.fullmatch(r'(db-doc-[0-9]+)-p[0-9]+', source.id)
        group = match.group(1) if match else source.id
        if group in seen:
            repeats.append(source)
        else:
            seen.add(group)
            distinct.append(source)
    sources = (distinct + repeats)[:3]
    if not sources:
        return KnowledgeAnswer('No answer was found in the accessible sample documents.', [], 'abstain')

    extractive = '\n\n'.join(f'[{s.id}] {s.text}' for s in sources)
    if enable_groq:
        try:
            answer = generate_with_groq(question, [
                {'id': s.id, 'title': s.title, 'text': s.text[:4000]} for s in sources
            ])
            if not _has_valid_citations(answer, sources):
                return KnowledgeAnswer(
                    extractive, sources, 'extractive',
                    'AI citations could not be verified; showing source excerpts.',
                )
            return KnowledgeAnswer(
                answer, sources, 'groq',
                'AI-generated; valid citation IDs do not establish factual accuracy. Verify the sources.',
            )
        except ProviderUnavailable:
            return KnowledgeAnswer(extractive, sources, 'extractive', 'AI service unavailable; showing source excerpts.')
    return KnowledgeAnswer(extractive, sources, 'extractive')
