"""PostgreSQL vector storage for explicitly authorized source references.

The caller must derive permitted IDs and revision hashes from current, active
documents and recheck them before returning text. This module stores embeddings
and opaque references, not source text. It is not an authentication boundary.
"""

import math
import re
from collections.abc import Mapping, Sequence

from pgvector import Vector
from pgvector.psycopg import register_vector

DIMENSIONS = 384
DEFAULT_MODEL = 'multilingual-e5-small-q8-dd476dd0'
_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')


def _vector(values: Sequence[float]) -> Vector:
    if len(values) != DIMENSIONS:
        raise ValueError('Embedding has the wrong dimensions')
    floats = [float(value) for value in values]
    if any(not math.isfinite(value) for value in floats) or not any(floats):
        raise ValueError('Embedding must be finite and nonzero')
    return Vector(floats)


def _reference(source_id: str, digest: str) -> None:
    if not isinstance(source_id, str) or not _NAME.fullmatch(source_id):
        raise ValueError('Invalid source reference')
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError('Invalid source revision')


class PostgresVectorIndex:
    def __init__(self, connection, namespace: str, model: str = DEFAULT_MODEL):
        if not _NAME.fullmatch(namespace) or not _NAME.fullmatch(model):
            raise ValueError('Invalid index namespace or model')
        self.connection = connection
        self.namespace = namespace
        self.model = model

    def initialize(self) -> None:
        # The operator provisions the extension; the application never elevates
        # privileges to create it. Register on each separate connection.
        register_vector(self.connection)
        self.connection.execute('''CREATE TABLE IF NOT EXISTS tawzeef_vectors (
            namespace text NOT NULL,
            model text NOT NULL,
            source_id text NOT NULL,
            revision text NOT NULL CHECK (length(revision) = 64),
            embedding vector(384) NOT NULL,
            PRIMARY KEY (namespace, model, source_id)
        )''')

    def replace(self, rows: Sequence[tuple[str, str, Sequence[float]]]) -> None:
        prepared = []
        seen = set()
        for source_id, revision, values in rows:
            _reference(source_id, revision)
            if source_id in seen:
                raise ValueError('Duplicate source reference')
            seen.add(source_id)
            prepared.append((self.namespace, self.model, source_id, revision, _vector(values)))
        with self.connection.transaction():
            self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                                    (f'tawzeef-index:{self.namespace}:{self.model}',))
            with self.connection.cursor() as cursor:
                cursor.executemany('''INSERT INTO tawzeef_vectors
                    (namespace, model, source_id, revision, embedding) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (namespace, model, source_id) DO UPDATE
                    SET revision = EXCLUDED.revision, embedding = EXCLUDED.embedding''', prepared)
            self.connection.execute('''DELETE FROM tawzeef_vectors
                WHERE namespace = %s AND model = %s AND source_id <> ALL(%s::text[])''',
                (self.namespace, self.model, list(seen)))

    def search(self, allowed: Mapping[str, str], query: Sequence[float], limit: int = 3) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError('Invalid result limit')
        query_vector = _vector(query)
        for source_id, revision in allowed.items():
            _reference(source_id, revision)
        if not allowed:
            return []
        rows = self.connection.execute('''WITH permitted AS (
            SELECT unnest(%s::text[]) AS source_id, unnest(%s::text[]) AS revision
        )
        SELECT v.source_id, v.revision, v.embedding <=> %s AS distance
        FROM tawzeef_vectors v JOIN permitted p
          ON p.source_id = v.source_id AND p.revision = v.revision
        WHERE v.namespace = %s AND v.model = %s
        ORDER BY distance, v.source_id LIMIT %s''',
            (list(allowed), list(allowed.values()), query_vector, self.namespace, self.model, limit))
        return [{'source_id': row[0], 'revision': row[1], 'distance': row[2]} for row in rows]
