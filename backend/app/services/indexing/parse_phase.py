"""Parse phase: turn each stored source file into symbols, references and chunks.

Resumable: files are processed in id order and the last finished file id is kept in the job
cursor. Each tick parses files for a bounded time, then returns; the runner commits the rows
and the new cursor in one transaction, so a crash loses only the unfinished tick. Re-running a
file first deletes its old rows, so a restarted phase never duplicates anything.
"""

import time
from typing import Any

from sqlalchemy import delete, insert, select, update

from app.core.errors import RepoTooLarge
from app.db.models import Chunk, CodeRef, File, Symbol
from app.services.indexing.runner import PhaseContext, PhaseResult, register
from app.services.parsing.parse import parse_source
from app.services.security.secrets import redact

_COUNTERS = ("files", "symbols", "chunks", "refs", "syntax_error_files", "truncated_files")


def _parse_one(ctx: PhaseContext, f: File, totals: dict[str, int]) -> None:
    db, s = ctx.db, ctx.settings
    assert f.content is not None and f.language is not None

    # Rows stored before secret redaction existed (or by an older version) are cleaned here too.
    cleaned = redact(f.content)
    if cleaned.count:
        f.content = cleaned.text
        f.redactions = max(f.redactions, cleaned.count)

    for model in (Chunk, CodeRef, Symbol):  # idempotent: drop anything a previous attempt wrote
        db.execute(delete(model).where(model.file_id == f.id))

    result = parse_source(f.path, f.language, f.content, s.chunk_max_chars, s.chunk_merge_below)
    parsed = result.parsed
    lines = f.content.split("\n")

    symbol_ids: list[int] = []
    if parsed.symbols:
        rows: list[dict[str, Any]] = [
            {
                "snapshot_id": ctx.snapshot.id,
                "file_id": f.id,
                "kind": sym.kind,
                "name": sym.name,
                "qualified_name": sym.qualified_name,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "signature": sym.signature,
            }
            for sym in parsed.symbols
        ]
        symbol_ids = list(
            db.execute(
                insert(Symbol).returning(Symbol.id, sort_by_parameter_order=True), rows
            ).scalars()
        )
        links = [
            {"id": symbol_ids[i], "parent_id": symbol_ids[sym.parent]}
            for i, sym in enumerate(parsed.symbols)
            if sym.parent is not None
        ]
        if links:
            db.execute(update(Symbol), links)  # ORM bulk update by primary key

    def symbol_id(index: int | None) -> int | None:
        return symbol_ids[index] if index is not None else None

    if result.chunks:
        db.execute(
            insert(Chunk),
            [
                {
                    "snapshot_id": ctx.snapshot.id,
                    "file_id": f.id,
                    "symbol_id": symbol_id(c.symbol),
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "text": "\n".join(lines[c.start_line - 1 : c.end_line]),
                }
                for c in result.chunks
            ],
        )
    if parsed.refs:
        db.execute(
            insert(CodeRef),
            [
                {
                    "snapshot_id": ctx.snapshot.id,
                    "file_id": f.id,
                    "symbol_id": symbol_id(r.symbol),
                    "kind": r.kind,
                    "name": r.name,
                    "line": r.line,
                    "detail": r.detail,
                }
                for r in parsed.refs
            ],
        )

    f.parse_status = (
        "truncated" if parsed.truncated else "syntax_errors" if parsed.syntax_errors else "ok"
    )
    totals["files"] += 1
    totals["symbols"] += len(parsed.symbols)
    totals["chunks"] += len(result.chunks)
    totals["refs"] += len(parsed.refs)
    totals["syntax_error_files"] += int(parsed.syntax_errors)
    totals["truncated_files"] += int(parsed.truncated)
    if totals["chunks"] > s.max_chunks:
        raise RepoTooLarge(
            f"This repository would produce more than {s.max_chunks} chunks. "
            "Try a smaller repository."
        )


@register("parse")
def run_parse(ctx: PhaseContext) -> PhaseResult:
    s = ctx.settings
    previous: dict[str, Any] = (ctx.job.cursor or {}).get("parse", {})
    last_id = int(previous.get("last_file_id", 0))
    totals = {name: int(previous.get(name, 0)) for name in _COUNTERS}
    deadline = min(ctx.deadline, time.monotonic() + s.parse_budget_seconds)
    parsed_this_tick = 0
    finished = False

    while True:
        batch = list(
            ctx.db.scalars(
                select(File)
                .where(
                    File.snapshot_id == ctx.snapshot.id,
                    File.id > last_id,
                    File.content.is_not(None),
                    File.language.is_not(None),
                )
                .order_by(File.id)
                .limit(s.parse_batch_files)
            )
        )
        if not batch:
            finished = True
            break
        out_of_time = False
        for f in batch:
            # Always make progress (at least one file per tick), then respect the time budget.
            if parsed_this_tick > 0 and time.monotonic() > deadline:
                out_of_time = True
                break
            _parse_one(ctx, f, totals)
            last_id = f.id
            parsed_this_tick += 1
        if out_of_time:
            break

    ctx.snapshot.chunk_count = totals["chunks"]
    cursor = {"parse": {**totals, "last_file_id": last_id, "done": finished}}
    return PhaseResult(next_phase="embed" if finished else None, cursor=cursor)
