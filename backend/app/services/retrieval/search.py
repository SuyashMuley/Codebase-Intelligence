"""Keyword, vector and hybrid search over one snapshot's chunks."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import exists, select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import Chunk, File, Symbol
from app.providers.embeddings.base import Embedder
from app.services.retrieval.fusion import reciprocal_rank_fusion
from app.services.retrieval.query import MAX_QUERY_CHARS, query_terms, symbol_names, to_tsquery

Mode = Literal["keyword", "vector", "hybrid"]
CANDIDATES = 50  # how many results each method contributes before fusion

_PATH_TOKENS = "to_tsvector('simple', regexp_replace(f.path, '[^A-Za-z0-9]+', ' ', 'g'))"

# Text rank, plus a smaller bonus when the file path itself matches (paths name things), plus a
# "jump to definition" bonus: a chunk that contains the first line of a symbol whose name appears
# in the question (so asking about `parse_header_links` puts its definition above its callers).
_NAME_BONUS = """
             + :name_bonus * CASE WHEN EXISTS (
                   SELECT 1 FROM symbols s
                   WHERE s.file_id = c.file_id
                     AND s.start_line BETWEEN c.start_line AND c.end_line
                     AND lower(s.name) = ANY(CAST(:names AS text[]))
               ) THEN 1 ELSE 0 END"""
_KEYWORD_SQL = text(
    f"""
    WITH q AS (SELECT to_tsquery('simple', :query) AS tsq)
    SELECT c.id,
           ts_rank_cd(c.tsv, q.tsq, 32)
             + 0.4 * ts_rank_cd({_PATH_TOKENS}, q.tsq, 32){_NAME_BONUS} AS score
    FROM chunks c
    JOIN files f ON f.id = c.file_id
    CROSS JOIN q
    WHERE c.snapshot_id = :sid
      AND (c.tsv @@ q.tsq OR {_PATH_TOKENS} @@ q.tsq)
    ORDER BY score DESC, c.id
    LIMIT :limit
    """
)


@dataclass
class Hit:
    chunk_id: int
    path: str
    start_line: int
    end_line: int
    text: str
    symbol: str | None  # qualified name of the chunk's primary symbol
    score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None


@dataclass
class SearchResult:
    hits: list[Hit]
    mode_used: Mode
    warnings: list[str] = field(default_factory=list)


SYMBOL_BONUS = 1.0  # larger than any text rank (which is below 1), so definitions lead


def keyword_search(
    db: Session,
    snapshot_id: uuid.UUID,
    query: str,
    limit: int,
    symbol_boost: bool = True,
) -> list[int]:
    terms = query_terms(query)
    if not terms:
        return []
    names = symbol_names(query) if symbol_boost else []
    rows = db.execute(
        _KEYWORD_SQL,
        {
            "query": to_tsquery(terms),
            "sid": snapshot_id,
            "limit": limit,
            "names": names,
            "name_bonus": SYMBOL_BONUS if names else 0.0,
        },
    ).all()
    return [int(r[0]) for r in rows]


def vector_search(
    db: Session, snapshot_id: uuid.UUID, query_vector: list[float], limit: int
) -> list[int]:
    """Exact nearest neighbours by cosine distance, restricted to this snapshot."""
    distance = Chunk.embedding.cosine_distance(query_vector)
    rows = db.execute(
        select(Chunk.id)
        .where(Chunk.snapshot_id == snapshot_id, Chunk.embedding.is_not(None))
        .order_by(distance, Chunk.id)
        .limit(limit)
    ).all()
    return [int(r[0]) for r in rows]


def _has_embeddings(db: Session, snapshot_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(
            select(exists().where(Chunk.snapshot_id == snapshot_id, Chunk.embedding.is_not(None)))
        )
    )


def search(
    db: Session,
    snapshot_id: uuid.UUID,
    query: str,
    mode: Mode,
    limit: int,
    embedder: Embedder | None,
    strict: bool = False,
    symbol_boost: bool = True,
    weights: Mapping[str, float] | None = None,
) -> SearchResult:
    """Search one snapshot.

    Hybrid and vector searches need embeddings. If they are missing (indexing is still running)
    or the embedding service fails, a normal search falls back to keyword search and says so in
    `warnings`. With `strict=True` (the benchmark) any such problem raises instead, so a result
    is never quietly produced by a different method than the one asked for.
    """
    query = query.strip()[:MAX_QUERY_CHARS]
    warnings: list[str] = []
    keyword_ids: list[int] = []
    vector_ids: list[int] = []

    if mode in ("keyword", "hybrid"):
        keyword_ids = keyword_search(db, snapshot_id, query, CANDIDATES, symbol_boost)

    if mode in ("vector", "hybrid"):
        if embedder is None or not _has_embeddings(db, snapshot_id):
            if strict:
                raise RuntimeError("this snapshot has no embeddings")
            warnings.append("embeddings_not_ready")
        else:
            try:
                vector_ids = vector_search(db, snapshot_id, embedder.embed_query(query), CANDIDATES)
            except AppError as exc:
                if strict:
                    raise
                warnings.append(f"embedding_failed:{exc.code}")

    used: Mode = mode
    if mode in ("vector", "hybrid") and warnings:  # the vector side was unavailable
        used = "keyword"
        if mode == "vector":
            keyword_ids = keyword_search(db, snapshot_id, query, CANDIDATES, symbol_boost)

    fused = reciprocal_rank_fusion({"keyword": keyword_ids, "vector": vector_ids}, weights=weights)
    top = fused[:limit]
    return SearchResult(hits=_load(db, top), mode_used=used, warnings=warnings)


def _load(db: Session, top: list[tuple[int, float, dict[str, int]]]) -> list[Hit]:
    if not top:
        return []
    ids = [i for i, _s, _r in top]
    rows = db.execute(
        select(Chunk, File.path, Symbol.qualified_name)
        .join(File, File.id == Chunk.file_id)
        .outerjoin(Symbol, Symbol.id == Chunk.symbol_id)
        .where(Chunk.id.in_(ids))
    ).all()
    by_id = {c.id: (c, path, sym) for c, path, sym in rows}
    hits: list[Hit] = []
    for chunk_id, score, ranks in top:
        chunk, path, sym = by_id[chunk_id]
        hits.append(
            Hit(
                chunk_id=chunk_id,
                path=path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                text=chunk.text,
                symbol=sym.split("::", 1)[1] if sym else None,
                score=score,
                keyword_rank=ranks.get("keyword"),
                vector_rank=ranks.get("vector"),
            )
        )
    return hits
