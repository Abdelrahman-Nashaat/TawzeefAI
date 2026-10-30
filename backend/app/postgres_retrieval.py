"""Experimental hybrid retrieval over current SQLite documents and pgvector.

Only persisted knowledge documents are included here; jobs and fixtures remain
in the regular lexical search. The server must derive role from its session.
Returned excerpts are candidates, not verified answers. Public answer and chat
paths may opt in experimentally, pending evaluation of relevance and failure behavior.
"""

import hashlib
import json
import re
from pathlib import Path

from app.knowledge import ROLE_AUDIENCES, _document_passages, _rank_lexical, _tokens
from app.knowledge_documents import fetch_authorized_documents
from app.postgres_vectors import PostgresVectorIndex

MAX_PASSAGES = 100
COLLECTIONS = frozenset(('legacy_unclassified', 'handbook', 'job_description', 'past_hire_patterns'))


def _revision(source) -> str:
    values = [source.id, source.title, source.text, source.page]
    if source.section is not None:
        values.append(source.section)
    data = json.dumps(values, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def search_postgres(path, role, question, embedder, connection, *, min_similarity=0.85, limit=3,
                    collection=None, job_id=None, section=None):
    if role not in ROLE_AUDIENCES:
        raise ValueError('Unsupported role')
    if not isinstance(question, str) or len(question) > 2000:
        raise ValueError('Question must be at most 2000 characters')
    if collection is not None and collection not in COLLECTIONS:
        raise ValueError('Unsupported collection')
    if job_id is not None and (not isinstance(job_id, str) or len(job_id) > 64 or
                               not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', job_id)):
        raise ValueError('Invalid job identifier')
    if section is not None:
        if not isinstance(section, str) or any(ord(char) < 32 for char in section):
            raise ValueError('Invalid section')
        section = section.strip()
        if not 1 <= len(section) <= 200:
            raise ValueError('Invalid section')
    if not 0 <= min_similarity <= 1 or not 1 <= limit <= 10:
        raise ValueError('Invalid search settings')
    if not question.strip():
        return {'status': 'abstain', 'sources': []}
    db_id = hashlib.sha256(str(Path(path).resolve()).encode('utf-8')).hexdigest()[:40]
    # Separate namespaces prevent a filtered refresh from deleting another
    # collection or the backwards-compatible unfiltered index.
    namespace = f'db-{db_id}-{role}' + (f'-collection-{collection}' if collection else '')
    if job_id is not None:
        namespace += '-job-' + hashlib.sha256(job_id.encode('utf-8')).hexdigest()[:16]
    if section is not None:
        # Hash the entire filter tuple for a bounded name even with the
        # longest collection and job combination. Legacy namespaces remain
        # unchanged when no section filter is requested.
        scope = json.dumps([collection, job_id, section], ensure_ascii=False, separators=(',', ':'))
        namespace = f'db-{db_id}-{role}-section-' + hashlib.sha256(scope.encode('utf-8')).hexdigest()[:32]
    index = PostgresVectorIndex(connection, namespace)
    index.initialize()
    documents = fetch_authorized_documents(path, role)
    if collection is not None:
        documents = [doc for doc in documents if doc['collection'] == collection]
    if job_id is not None:
        documents = [doc for doc in documents if doc['job_id'] == job_id]
    pairs = [(doc, source) for doc in documents for source in _document_passages(doc)]
    if section is not None:
        pairs = [(doc, source) for doc, source in pairs if source.section == section]
    if len(pairs) > MAX_PASSAGES:
        raise ValueError('Too many passages for experimental search')
    if not pairs:
        index.replace([])
        return {'status': 'abstain', 'sources': []}
    vectors = embedder.encode([source.text for _, source in pairs], query=False)
    if len(vectors) != len(pairs):
        raise ValueError('Embedding count does not match source count')
    query = embedder.encode([question], query=True)
    if len(query) != 1:
        raise ValueError('Invalid query embedding')
    live = {document['id']: document for document in fetch_authorized_documents(path, role)}
    current = [(doc, source, vector) for (doc, source), vector in zip(pairs, vectors, strict=True)
               if live.get(doc['id']) == doc]
    index.replace([(source.id, _revision(source), vector) for _, source, vector in current])
    allowed = {source.id: _revision(source) for _, source, _ in current}
    nearest = index.search(allowed, query[0], limit=max(1, len(allowed)))
    lexical = _rank_lexical(_tokens(question), [source for _, source, _ in current])
    lexical_ranks = {source.id: rank for rank, (_, source) in enumerate(lexical, 1)}
    candidates = []
    for rank, row in enumerate(nearest, 1):
        source_id = row['source_id']
        similarity = 1 - row['distance']
        if similarity < min_similarity and source_id not in lexical_ranks:
            continue
        fusion = 1 / (60 + rank)
        if source_id in lexical_ranks:
            fusion += 1 / (60 + lexical_ranks[source_id])
        candidates.append((fusion, similarity, source_id))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    by_id = {source.id: (doc, source) for doc, source, _ in current}
    live = {document['id']: document for document in fetch_authorized_documents(path, role)}
    result = []
    for _, similarity, source_id in candidates:
        document, source = by_id[source_id]
        if live.get(document['id']) != document:
            continue
        result.append({'document_id': document['id'], 'id': source.id, 'title': source.title,
                       'page': source.page, 'text': source.text, 'similarity': similarity,
                       'collection': document['collection'], 'job_id': document['job_id'], 'section': source.section})
        if len(result) == limit:
            break
    return {'status': 'ok' if result else 'abstain', 'sources': result,
            'notice': 'Experimental retrieved candidates; similarity does not establish factual support.'}
