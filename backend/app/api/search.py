"""Search an indexed snapshot: keyword, vector, or both fused by rank."""

from typing import Any

from fastapi import Depends

from app.api.deps import (
    DbSession,
    OwnedRepo,
    Providers,
    csrf_guard,
    ip_backstop,
    user_general_limit,
    user_search_limit,
)
from app.core.dbrelease import ReleasingRouter
from app.schemas.repository import ErrorEnvelope
from app.schemas.search import SearchHitOut, SearchOut, SearchRequest
from app.services.indexing import queries
from app.services.retrieval.search import search

router = ReleasingRouter(
    prefix="/api/repositories/{repository_id}",
    tags=["search"],
    dependencies=[
        Depends(ip_backstop),
        Depends(csrf_guard),
        Depends(user_general_limit),
        Depends(user_search_limit),
    ],
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in"},
    404: {"model": ErrorEnvelope, "description": "Repository or snapshot not found"},
    409: {"model": ErrorEnvelope, "description": "Nothing has been indexed yet"},
    429: {"model": ErrorEnvelope, "description": "Too many searches"},
}


@router.post("/search", response_model=SearchOut, responses=_ERRORS)
def search_code(
    body: SearchRequest,
    repo: OwnedRepo,
    db: DbSession,
    providers: Providers,
) -> SearchOut:
    """Find the code chunks most relevant to a question.

    `vector` (default) ranks by meaning. It scored best on both benchmark repositories; `hybrid`
    (rank fusion with keyword search) did not beat it, see docs/PLAN.md. If embeddings are not
    ready or the embedding service is down, the answer falls back to keyword search and
    `warnings` says so, rather than failing.
    """
    # Keyword search sends nothing anywhere. Meaning search sends the question to an embedding
    # provider: the platform's for a public repository, the owner's own for a private one.
    embedder = None if body.mode == "keyword" else providers.embedder()
    snapshot = queries.resolve_snapshot(db, repo.id, body.snapshot)
    result = search(db, snapshot.id, body.query, body.mode, body.limit, embedder)
    return SearchOut(
        mode_used=result.mode_used,
        warnings=result.warnings,
        hits=[
            SearchHitOut(
                chunk_id=h.chunk_id,
                path=h.path,
                start_line=h.start_line,
                end_line=h.end_line,
                symbol=h.symbol,
                text=h.text,
                score=round(h.score, 6),
                keyword_rank=h.keyword_rank,
                vector_rank=h.vector_rank,
            )
            for h in result.hits
        ],
    )
