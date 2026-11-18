"""Find the code a question is about, and decide whether there is enough of it to answer from.

Shared by the question-answering workflow and the offline evaluation, so what is measured is what
runs. The decision rule, in one place:

    enough evidence  =  something was retrieved
                        AND ( the question names a function, method or class that exists in
                              this snapshot
                              OR the closest chunk is at least `qa_min_similarity` similar )
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import Symbol
from app.providers.embeddings.base import Embedder
from app.services.retrieval.fusion import reciprocal_rank_fusion
from app.services.retrieval.search import Hit, keyword_search, load_hits, vector_search_scored

KEYWORD_CANDIDATES = 8


@dataclass
class Retrieval:
    hits: list[Hit] = field(default_factory=list)
    top_similarity: float | None = None
    symbol_hit: bool = False
    mode: str = "keyword"
    reason: str = ""  # why retrieval could not be trusted, if it could not


def retrieve(
    db: Session,
    snapshot_id: uuid.UUID,
    embedder: Embedder | None,
    embeddings_ready: bool,
    search_query: str,
    names: list[str],
    k: int,
) -> Retrieval:
    sims: dict[int, float] = {}
    vector_ids: list[int] = []
    reason = ""
    if embeddings_ready and embedder is not None:
        try:
            scored = vector_search_scored(db, snapshot_id, embedder.embed_query(search_query), k)
            sims = dict(scored)
            vector_ids = [i for i, _ in scored]
        except AppError as exc:
            reason = f"embedding_failed:{exc.code}"
    else:
        reason = "embeddings_not_ready"
    # A question that names code is also searched by name: exact names beat meaning.
    keyword_ids = (
        keyword_search(db, snapshot_id, search_query, KEYWORD_CANDIDATES)
        if names or not vector_ids
        else []
    )
    ranked = reciprocal_rank_fusion({"vector": vector_ids, "keyword": keyword_ids})
    hits = load_hits(db, ranked[:k])
    symbol_hit = bool(names) and bool(
        db.scalar(
            select(Symbol.id)
            .where(Symbol.snapshot_id == snapshot_id, func.lower(Symbol.name).in_(names))
            .limit(1)
        )
    )
    if vector_ids and keyword_ids:
        mode = "vector+keyword"
    else:
        mode = "vector" if vector_ids else "keyword"
    return Retrieval(
        hits=hits,
        top_similarity=max(sims.values()) if sims else None,
        symbol_hit=symbol_hit,
        mode=mode,
        reason=reason,
    )


def is_sufficient(r: Retrieval, settings: Settings) -> bool:
    return bool(r.hits) and (
        r.symbol_hit
        or (r.top_similarity is not None and r.top_similarity >= settings.qa_min_similarity)
    )


def is_strong(r: Retrieval, settings: Settings) -> bool:
    return r.symbol_hit or (
        r.top_similarity is not None and r.top_similarity >= settings.qa_high_similarity
    )
