"""Embed phase: give every chunk a vector.

Resumable by construction: the work list is "chunks with no vector yet", so a crashed or timed-out
tick just leaves those chunks for the next one. Each tick embeds for a bounded time; the runner
commits the vectors and the progress together.
"""

import time
from typing import Any

from sqlalchemy import exists, func, select, update

from app.core.errors import EmbeddingNotConfigured, ProvidersRequired
from app.db.models import Chunk, File
from app.providers.embeddings.factory import embedding_model_id, get_embedder
from app.services.indexing.runner import PhaseContext, PhaseResult, register
from app.services.retrieval.documents import document_text


@register("embed")
def run_embed(ctx: PhaseContext) -> PhaseResult:
    db, s = ctx.db, ctx.settings
    sid = ctx.snapshot.id
    previous: dict[str, Any] = (ctx.job.cursor or {}).get("embed", {})

    # One model identifier is used for everything below: the embedder's own when it exists, so
    # what is stored and what is compared against can never disagree (a mismatch would make each
    # tick discard the previous tick's vectors). Without credentials we fall back to the
    # configured id, which is enough to notice stale vectors and to finish if nothing is pending.
    missing: EmbeddingNotConfigured | None = None
    if ctx.repository.is_private and ctx.embedder is None:
        # Whoever drives the job must supply the owner's own embedder: a private repository is
        # never embedded with the platform's, whatever the configuration.
        raise ProvidersRequired(
            "A private repository can only be embedded with its owner's own provider."
        )
    try:
        embedder = ctx.embedder or get_embedder(s)
        model = embedder.model_id
    except EmbeddingNotConfigured as exc:
        embedder, missing, model = None, exc, embedding_model_id(s)

    # Vectors from a different model (settings changed) are not comparable: drop and redo them.
    db.execute(
        update(Chunk)
        .where(
            Chunk.snapshot_id == sid,
            Chunk.embedding_model.is_not(None),
            Chunk.embedding_model != model,
        )
        .values(embedding=None, embedding_model=None)
    )

    pending = db.scalar(select(exists().where(Chunk.snapshot_id == sid, Chunk.embedding.is_(None))))
    neurons = float(previous.get("neurons", 0.0))
    finished = not pending

    if pending:
        if embedder is None:
            raise missing or EmbeddingNotConfigured("Embeddings are not configured.")
        before = dict(embedder.usage)
        deadline = min(ctx.deadline, time.monotonic() + s.embedding_budget_seconds)
        done_this_tick = 0
        while True:
            rows = db.execute(
                select(Chunk.id, Chunk.text, File.path)
                .join(File, File.id == Chunk.file_id)
                .where(Chunk.snapshot_id == sid, Chunk.embedding.is_(None))
                .order_by(Chunk.id)
                .limit(embedder.batch_size)
            ).all()
            if not rows:
                finished = True
                break
            if done_this_tick > 0 and time.monotonic() > deadline:
                break  # out of time: the remaining chunks are picked up by the next tick
            vectors = embedder.embed_documents(
                [document_text(path, text) for _, text, path in rows]
            )
            db.execute(
                update(Chunk),
                [
                    {"id": chunk_id, "embedding": vector, "embedding_model": model}
                    for (chunk_id, _text, _path), vector in zip(rows, vectors, strict=True)
                ],
            )
            done_this_tick += len(rows)
        neurons += embedder.usage.get("neurons", 0.0) - before.get("neurons", 0.0)

    total = db.scalar(select(func.count()).where(Chunk.snapshot_id == sid)) or 0
    embedded = (
        db.scalar(
            select(func.count()).where(Chunk.snapshot_id == sid, Chunk.embedding.is_not(None))
        )
        or 0
    )
    cursor = {
        "embed": {
            "embedded": embedded,
            "total": total,
            "model": model,
            "done": finished,
            "neurons": round(neurons, 3),
        }
    }
    return PhaseResult(next_phase="graph" if finished else None, cursor=cursor)
