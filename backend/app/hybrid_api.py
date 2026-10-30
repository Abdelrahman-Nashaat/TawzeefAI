"""Explicit opt-in endpoint for the local PostgreSQL retrieval experiment."""

import os
from functools import lru_cache
from threading import BoundedSemaphore
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import User, get_current_user
from app.knowledge_documents import Collection

router = APIRouter(prefix='/api/knowledge')
_busy = BoundedSemaphore(1)


class HybridQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=2000)
    collection: Collection | None = None


@lru_cache(maxsize=1)
def _load_model(directory: str):
    from app.semantic_experiment import LocalE5

    return LocalE5(directory)


@router.get('/hybrid-status')
def hybrid_status(user: Annotated[User, Depends(get_current_user)]):
    # Configuration only: the model and database may still be unavailable.
    return {'configured': os.environ.get('TAWZEEF_ENABLE_HYBRID') == '1'
            and bool(os.environ.get('TAWZEEF_EMBEDDING_MODEL_DIR'))}


def retrieve_hybrid(path, role: str, question: str) -> dict:
    """Run a bounded hybrid search. Callers must recheck identities and sources."""
    directory = os.environ.get('TAWZEEF_EMBEDDING_MODEL_DIR', '')
    if os.environ.get('TAWZEEF_ENABLE_HYBRID') != '1' or not directory:
        raise RuntimeError('Hybrid search is not configured')
    if not _busy.acquire(blocking=False):
        raise RuntimeError('Hybrid search is busy')
    try:
        import psycopg

        from app.postgres_retrieval import search_postgres

        model = _load_model(directory)
        with psycopg.connect(autocommit=True, connect_timeout=3) as connection:
            return search_postgres(path, role, question, model, connection)
    finally:
        _busy.release()


@router.post('/experimental-hybrid')
def hybrid_question(payload: HybridQuestion, request: Request,
                    user: Annotated[User, Depends(get_current_user)]):
    directory = os.environ.get('TAWZEEF_EMBEDDING_MODEL_DIR', '')
    if os.environ.get('TAWZEEF_ENABLE_HYBRID') != '1' or not directory:
        raise HTTPException(status_code=503, detail='Experimental hybrid search is not configured')
    if not _busy.acquire(blocking=False):
        raise HTTPException(status_code=429, detail='Experimental search is busy; retry later')
    try:
        try:
            import psycopg

            from app.postgres_retrieval import search_postgres

            model = _load_model(directory)
            with psycopg.connect(autocommit=True, connect_timeout=3) as connection:
                result = search_postgres(request.app.state.database_path, user.role,
                                         payload.question, model, connection, collection=payload.collection)
        except Exception as exc:
            # Database DSNs, local paths and documents must not be returned.
            raise HTTPException(status_code=503, detail='Experimental hybrid search is unavailable') from exc
        current = get_current_user(request)
        if current.id != user.id or current.role != user.role:
            raise HTTPException(status_code=401, detail='Session changed during search')
        return result
    finally:
        _busy.release()
