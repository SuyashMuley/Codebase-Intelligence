"""Graph phase: resolve imports and calls into edges.

Everything is computed in memory from rows the earlier phases stored, so the phase is a pure
function of the snapshot: it deletes the snapshot's edges and writes them again, which makes a
restarted phase safe. The runner commits the edges and the cursor in one transaction.
"""

import re
from collections import Counter
from typing import Any

from sqlalchemy import delete, func, insert, select

from app.db.models import CodeRef, File, GraphEdge, Symbol
from app.services.graph.builder import GraphBuilder
from app.services.graph.model import FileInfo, RefInfo, SymInfo
from app.services.indexing.runner import PhaseContext, PhaseResult, register

_BATCH = 5_000
_GITHUB_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_GO_PACKAGE = re.compile(r"(?m)^package\s+(\w+)")


@register("graph")
def run_graph(ctx: PhaseContext) -> PhaseResult:
    db, sid = ctx.db, ctx.snapshot.id

    files = [
        FileInfo(
            id=row.id,
            path=row.path,
            language=row.language,
            go_package=_go_package(row.head) if row.language == "go" else None,
        )
        for row in db.execute(
            select(
                File.id, File.path, File.language, func.substr(File.content, 1, 4000).label("head")
            )
            .where(File.snapshot_id == sid, File.content.is_not(None))
            .order_by(File.id)
        )
    ]
    symbols = [
        SymInfo(
            id=r.id,
            file_id=r.file_id,
            kind=r.kind,
            name=r.name,
            parent_id=r.parent_id,
            signature=r.signature,
        )
        for r in db.execute(
            select(
                Symbol.id,
                Symbol.file_id,
                Symbol.kind,
                Symbol.name,
                Symbol.parent_id,
                Symbol.signature,
            )
            .where(Symbol.snapshot_id == sid)
            .order_by(Symbol.id)
        )
    ]
    refs = [
        RefInfo(
            file_id=r.file_id,
            symbol_id=r.symbol_id,
            kind=r.kind,
            name=r.name,
            line=r.line,
            detail=r.detail or {},
        )
        for r in db.execute(
            select(
                CodeRef.file_id,
                CodeRef.symbol_id,
                CodeRef.kind,
                CodeRef.name,
                CodeRef.line,
                CodeRef.detail,
            )  # fmt: skip
            .where(CodeRef.snapshot_id == sid)
            .order_by(CodeRef.id)
        )
    ]
    result = GraphBuilder(files, symbols, refs, _repo_full_name(ctx)).build()

    db.execute(delete(GraphEdge).where(GraphEdge.snapshot_id == sid))
    rows: list[dict[str, Any]] = [
        {
            "snapshot_id": sid,
            "src_file_id": e.src_file_id,
            "dst_file_id": e.dst_file_id,
            "src_symbol_id": e.src_symbol_id,
            "dst_symbol_id": e.dst_symbol_id,
            "kind": e.kind,
            "resolution": e.resolution,
            "target_name": e.target_name,
            "reason": e.reason,
            "line": e.line,
            "count": e.count,
        }
        for e in result.edges
    ]
    for i in range(0, len(rows), _BATCH):
        db.execute(insert(GraphEdge), rows[i : i + _BATCH])

    tally = Counter((e.kind, e.resolution) for e in result.edges)
    cursor = {
        "graph": {
            "files": len(files),
            "symbols": len(symbols),
            "static_imports": tally[("imports", "static")],
            "unresolved_imports": tally[("imports", "unresolved")],
            "static_calls": tally[("calls", "static")],
            "unresolved_calls": tally[("calls", "unresolved")],
            "truncated": result.truncated,
            "done": True,
        }
    }
    return PhaseResult(next_phase="done", cursor=cursor)


def _repo_full_name(ctx: PhaseContext) -> str:
    """`owner/name` as GitHub knows it (the Go module path of most repositories is
    `github.com/owner/name`). The stored URL is the source of truth."""
    match = _GITHUB_URL.match(ctx.repository.url or "")
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return f"{ctx.repository.owner}/{ctx.repository.name}"


def _go_package(head: str | None) -> str | None:
    match = _GO_PACKAGE.search(head or "")
    return match.group(1) if match else None
