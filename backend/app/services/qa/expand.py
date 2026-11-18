"""Add the code that the best search hits call or are called by, from the static graph.

Only `static` edges are used: a neighbour is included because the code proves the link, never
because a name looked similar.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Chunk, GraphEdge, Symbol
from app.services.retrieval.search import Hit, load_hits

SEED_HITS = 3


def graph_neighbours(db: Session, snapshot_id: uuid.UUID, hits: list[Hit], limit: int) -> list[Hit]:
    if limit <= 0:
        return []
    seeds = {h.symbol_id for h in hits[:SEED_HITS] if h.symbol_id is not None}
    if not seeds:
        return []
    edges = db.execute(
        select(GraphEdge.src_symbol_id, GraphEdge.dst_symbol_id, GraphEdge.count).where(
            GraphEdge.snapshot_id == snapshot_id,
            GraphEdge.kind == "calls",
            GraphEdge.resolution == "static",
            or_(GraphEdge.src_symbol_id.in_(seeds), GraphEdge.dst_symbol_id.in_(seeds)),
        )
    ).all()
    weight: dict[int, int] = {}
    for src, dst, count in edges:
        other = dst if src in seeds else src
        if other is not None and other not in seeds:
            weight[other] = weight.get(other, 0) + count
    if not weight:
        return []
    ranked = sorted(weight, key=lambda s: (-weight[s], s))

    already = {h.chunk_id for h in hits}
    chunk_ids: list[int] = []
    rows = db.execute(
        select(Symbol.id, Chunk.id)
        .join(
            Chunk,
            (Chunk.file_id == Symbol.file_id)
            & (Symbol.start_line >= Chunk.start_line)
            & (Symbol.start_line <= Chunk.end_line),
        )
        .where(Symbol.id.in_(ranked), Chunk.snapshot_id == snapshot_id)
    ).all()
    by_symbol: dict[int, int] = {}
    for sym_id, found in rows:
        by_symbol.setdefault(sym_id, found)
    for sym_id in ranked:
        candidate = by_symbol.get(sym_id)
        if candidate is not None and candidate not in already and candidate not in chunk_ids:
            chunk_ids.append(candidate)
        if len(chunk_ids) >= limit:
            break
    return load_hits(db, [(i, 0.0, {}) for i in chunk_ids])
