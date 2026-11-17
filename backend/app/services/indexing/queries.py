"""Read-side queries over an indexed snapshot (explorer, file viewer, statistics)."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import FileNotFound, SnapshotNotFound, SnapshotNotReady
from app.db.models import Chunk, CodeRef, File, IndexJob, Snapshot, Symbol
from app.schemas.files import (
    ChunkStats,
    Counts,
    FileDetail,
    FileEntry,
    FileList,
    FileStats,
    GraphStats,
    RedactionStats,
    RefStats,
    SnapshotRef,
    StatsOut,
    SymbolOut,
)

MAX_LISTED_FILES = 5_000


def resolve_snapshot(db: Session, repo_id: uuid.UUID, snapshot_id: uuid.UUID | None) -> Snapshot:
    """The requested snapshot of this repository, or its newest one. A snapshot id from another
    repository is treated as not found, so ids cannot be used to read someone else's data."""
    query = select(Snapshot).where(Snapshot.repository_id == repo_id)
    if snapshot_id is not None:
        snapshot = db.scalar(query.where(Snapshot.id == snapshot_id))
        if snapshot is None:
            raise SnapshotNotFound("That snapshot does not exist for this repository.")
        return snapshot
    snapshot = db.scalar(query.order_by(Snapshot.created_at.desc()).limit(1))
    if snapshot is None:
        raise SnapshotNotReady("Index this repository first.")
    return snapshot


def _ref(snapshot: Snapshot) -> SnapshotRef:
    return SnapshotRef(id=snapshot.id, commit_sha=snapshot.commit_sha, state=snapshot.status)


def list_files(db: Session, snapshot: Snapshot) -> FileList:
    counts = (
        select(Symbol.file_id, func.count().label("n"))
        .where(Symbol.snapshot_id == snapshot.id)
        .group_by(Symbol.file_id)
        .subquery()
    )
    rows = db.execute(
        select(File, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.file_id == File.id)
        .where(File.snapshot_id == snapshot.id)
        # Bytewise order, not the database locale, so the listing is identical everywhere.
        .order_by(File.path.collate("C"))
        .limit(MAX_LISTED_FILES + 1)
    ).all()
    items = [
        FileEntry(
            path=f.path,
            language=f.language,
            size_bytes=f.size_bytes,
            skipped_reason=f.skipped_reason,
            redactions=f.redactions,
            parse_status=f.parse_status,
            symbol_count=int(n),
        )
        for f, n in rows[:MAX_LISTED_FILES]
    ]
    return FileList(snapshot=_ref(snapshot), items=items, truncated=len(rows) > MAX_LISTED_FILES)


def get_file(db: Session, snapshot: Snapshot, path: str) -> FileDetail:
    # A plain equality lookup on stored paths: nothing here ever touches a filesystem.
    f = db.scalar(select(File).where(File.snapshot_id == snapshot.id, File.path == path))
    if f is None:
        raise FileNotFound("No such file in this snapshot.")
    symbols = db.scalars(
        select(Symbol).where(Symbol.file_id == f.id).order_by(Symbol.start_line, Symbol.id)
    ).all()
    return FileDetail(
        snapshot=_ref(snapshot),
        path=f.path,
        language=f.language,
        size_bytes=f.size_bytes,
        skipped_reason=f.skipped_reason,
        redactions=f.redactions,
        parse_status=f.parse_status,
        line_count=(f.content.count("\n") + 1) if f.content is not None else 0,
        content=f.content,
        symbols=[
            SymbolOut(
                id=s.id,
                kind=s.kind,
                name=s.name,
                qualified_name=s.qualified_name,
                start_line=s.start_line,
                end_line=s.end_line,
                parent_id=s.parent_id,
                signature=s.signature,
            )
            for s in symbols
        ],
    )


def _grouped(db: Session, column: Any, *where: Any) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).where(*where).group_by(column)).all()
    return {str(k): int(n) for k, n in rows if k is not None}


def build_stats(db: Session, snapshot: Snapshot) -> StatsOut:
    sid = snapshot.id
    job = db.scalar(select(IndexJob).where(IndexJob.snapshot_id == sid))
    cursor: dict[str, Any] = (job.cursor if job else None) or {}
    fetch = cursor.get("fetch") or {}
    parse = cursor.get("parse") or {}
    graph = cursor.get("graph")

    kept = (File.snapshot_id == sid, File.content.is_not(None))
    by_language = _grouped(db, File.language, *kept, File.language.is_not(None))
    stored = db.scalar(select(func.count()).where(File.snapshot_id == sid)) or 0
    total_bytes = db.scalar(select(func.coalesce(func.sum(File.size_bytes), 0)).where(*kept)) or 0
    symbol_kinds = _grouped(db, Symbol.kind, Symbol.snapshot_id == sid)
    chunk_total, chunk_avg, chunk_max = db.execute(
        select(
            func.count(),
            func.coalesce(func.avg(func.length(Chunk.text)), 0),
            func.coalesce(func.max(func.length(Chunk.text)), 0),
        ).where(Chunk.snapshot_id == sid)
    ).one()
    embedded_count, embedding_model = db.execute(
        select(func.count(), func.max(Chunk.embedding_model)).where(
            Chunk.snapshot_id == sid, Chunk.embedding.is_not(None)
        )
    ).one()
    ref_kinds = _grouped(db, CodeRef.kind, CodeRef.snapshot_id == sid)
    spans, redacted_files = db.execute(
        select(
            func.coalesce(func.sum(File.redactions), 0),
            func.count().filter(File.redactions > 0),
        ).where(File.snapshot_id == sid)
    ).one()
    syntax_error_files = (
        db.scalar(
            select(func.count()).where(
                File.snapshot_id == sid, File.parse_status.in_(("syntax_errors", "truncated"))
            )
        )
        or 0
    )

    return StatsOut(
        snapshot=_ref(snapshot),
        parsing_done=bool(parse.get("done", False)),
        files=FileStats(
            stored=int(stored),
            parsed=int(parse.get("files", 0)),
            bytes=int(total_bytes),
            by_language=dict(sorted(by_language.items())),
            skipped={str(k): int(v) for k, v in (fetch.get("skipped") or {}).items()},
        ),
        symbols=Counts(
            total=sum(symbol_kinds.values()), by_kind=dict(sorted(symbol_kinds.items()))
        ),
        chunks=ChunkStats(
            total=int(chunk_total),
            embedded=int(embedded_count),
            embedding_model=embedding_model,
            average_chars=int(chunk_avg),
            largest_chars=int(chunk_max),
        ),
        refs=RefStats(imports=ref_kinds.get("import", 0), calls=ref_kinds.get("call", 0)),
        graph=(
            GraphStats(
                static_imports=int(graph.get("static_imports", 0)),
                unresolved_imports=int(graph.get("unresolved_imports", 0)),
                static_calls=int(graph.get("static_calls", 0)),
                unresolved_calls=int(graph.get("unresolved_calls", 0)),
                truncated=bool(graph.get("truncated", False)),
            )
            if isinstance(graph, dict) and graph.get("done")
            else None
        ),
        redactions=RedactionStats(spans=int(spans), files=int(redacted_files)),
        syntax_error_files=int(syntax_error_files),
    )
