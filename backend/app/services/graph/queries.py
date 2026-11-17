"""Read side of the graph: file and symbol views, and impact analysis. Only `static` edges are
ever treated as facts; unresolved edges are shown as such or kept in a separate list."""

from collections import defaultdict
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import FileNotFound, GraphNotReady, SymbolNotFound
from app.db.models import File, GraphEdge, Snapshot, Symbol
from app.schemas.files import SnapshotRef
from app.schemas.graph import (
    Dependent,
    GraphEdgeOut,
    GraphNode,
    GraphOut,
    Hop,
    ImpactOut,
    ImpactTarget,
    SuggestedTestOut,
    UnverifiedCaller,
)
from app.services.graph.impact import ReverseGraph, path_to, reverse_bfs, suggest_tests

MAX_OVERVIEW_NODES = 60  # the whole-repository view: more than this is unreadable
MAX_NEIGHBOURHOOD_NODES = 300
MAX_GRAPH_EDGES = 1500
MAX_SYMBOL_EDGES = 600
MAX_GHOSTS = 40
MAX_DEPTH = 6
MAX_UNVERIFIED = 20

LIMITATIONS = (
    "Only imports and calls that name binding can prove are followed. Calls on variables "
    "(`obj.method()`), inherited methods, dynamic imports and path aliases are not, so a real "
    "dependent can be missing from this list.",
    "Everything listed under dependents is a static fact about the indexed commit. Everything "
    "under unverified callers and (when marked heuristic) suggested tests is not.",
    "A call at the top level of a file is attributed to the file, and what imports that file "
    "is not followed from a symbol's callers.",
)


def _ref(s: Snapshot) -> SnapshotRef:
    return SnapshotRef(id=s.id, commit_sha=s.commit_sha, state=s.status)


def require_built(snapshot: Snapshot) -> None:
    if snapshot.status != "ready":
        raise GraphNotReady("The dependency graph is built when indexing finishes.")


def _file_key(file_id: int) -> str:
    return f"file:{file_id}"


def _sym_key(symbol_id: int) -> str:
    return f"sym:{symbol_id}"


# --------------------------------------------------------------------------- file-level graph


def _file_edges(db: Session, sid: Any) -> list[tuple[int, int, str, int, int | None]]:
    """(src, dst, kind, count, line) between files. Imports win over calls for the same pair;
    a pair joined only by resolved calls (e.g. Go, same package) is kept as a `calls` edge."""
    imports = db.execute(
        select(GraphEdge.src_file_id, GraphEdge.dst_file_id, GraphEdge.count, GraphEdge.line).where(
            GraphEdge.snapshot_id == sid,
            GraphEdge.kind == "imports",
            GraphEdge.resolution == "static",
        )
    ).all()
    out: dict[tuple[int, int], tuple[int, int, str, int, int | None]] = {}
    for src, dst, count, line in imports:
        if src is not None and dst is not None and src != dst:
            out[(src, dst)] = (src, dst, "imports", count, line)
    calls = db.execute(
        select(GraphEdge.src_file_id, GraphEdge.dst_file_id, func.sum(GraphEdge.count))
        .where(
            GraphEdge.snapshot_id == sid,
            GraphEdge.kind == "calls",
            GraphEdge.resolution == "static",
            GraphEdge.src_file_id != GraphEdge.dst_file_id,
        )
        .group_by(GraphEdge.src_file_id, GraphEdge.dst_file_id)
    ).all()
    for src, dst, total in calls:
        if src is not None and dst is not None and (src, dst) not in out:
            out[(src, dst)] = (src, dst, "calls", int(total or 1), None)
    return list(out.values())


def file_graph(
    db: Session, snapshot: Snapshot, focus: str | None = None, depth: int = 1
) -> GraphOut:
    require_built(snapshot)
    sid = snapshot.id
    files = {
        f.id: f
        for f in db.scalars(select(File).where(File.snapshot_id == sid, File.language.is_not(None)))
    }
    edges = [e for e in _file_edges(db, sid) if e[0] in files and e[1] in files]
    indeg: dict[int, int] = defaultdict(int)
    outdeg: dict[int, int] = defaultdict(int)
    adjacency: dict[int, set[int]] = defaultdict(set)
    for src, dst, *_ in edges:
        outdeg[src] += 1
        indeg[dst] += 1
        adjacency[src].add(dst)
        adjacency[dst].add(src)

    note = None
    focus_id: int | None = None
    if focus is not None:
        match = next((f for f in files.values() if f.path == focus), None)
        if match is None:
            raise FileNotFound("That file does not exist in this snapshot.")
        focus_id = match.id
        keep = {focus_id}
        frontier = {focus_id}
        for _ in range(max(1, min(depth, 3))):
            frontier = {n for f in frontier for n in adjacency[f]} - keep
            keep |= frontier
        connected_total = len(keep)
    else:
        keep = {fid for fid in files if indeg[fid] or outdeg[fid]}
        connected_total = len(keep)
        isolated = len(files) - connected_total
        if isolated:
            note = (
                f"{isolated} file with no static links is not drawn."
                if isolated == 1
                else f"{isolated} files with no static links are not drawn."
            )

    truncated = False
    cap = MAX_OVERVIEW_NODES if focus_id is None else MAX_NEIGHBOURHOOD_NODES
    if len(keep) > cap:
        ranked = sorted(keep, key=lambda f: (-(indeg[f] + outdeg[f]), files[f].path))
        keep = set(ranked[:cap]) | ({focus_id} if focus_id is not None else set())
        truncated = True
    shown = [e for e in edges if e[0] in keep and e[1] in keep]
    if len(shown) > MAX_GRAPH_EDGES:
        shown = shown[:MAX_GRAPH_EDGES]
        truncated = True

    nodes = [
        GraphNode(
            id=_file_key(fid),
            kind="file",
            label=files[fid].path.rsplit("/", 1)[-1],
            path=files[fid].path,
            language=files[fid].language,
            in_degree=indeg[fid],
            out_degree=outdeg[fid],
            focus=fid == focus_id,
        )
        for fid in sorted(keep, key=lambda f: files[f].path)
    ]
    return GraphOut(
        snapshot=_ref(snapshot),
        level="file",
        nodes=nodes,
        edges=[
            GraphEdgeOut(
                source=_file_key(s),
                target=_file_key(d),
                kind=k,
                resolution="static",
                count=c,
                line=ln,
            )
            for s, d, k, c, ln in shown
        ],
        truncated=truncated,
        total_nodes=connected_total,
        note=note,
    )


# ------------------------------------------------------------------------- symbol-level graph


def symbol_graph(db: Session, snapshot: Snapshot, path: str) -> GraphOut:
    """The symbols of one file, who calls them and what they call. Calls that could not be
    resolved are drawn dashed towards a node named after what the code said."""
    require_built(snapshot)
    sid = snapshot.id
    file = db.scalar(select(File).where(File.snapshot_id == sid, File.path == path))
    if file is None:
        raise FileNotFound("That file does not exist in this snapshot.")
    own = {s.id: s for s in db.scalars(select(Symbol).where(Symbol.file_id == file.id))}

    rows = db.scalars(
        select(GraphEdge)
        .where(
            GraphEdge.snapshot_id == sid,
            GraphEdge.kind == "calls",
            or_(
                GraphEdge.src_file_id == file.id,
                GraphEdge.dst_symbol_id.in_(own.keys() or [-1]),
            ),
        )
        .order_by(GraphEdge.resolution.desc(), GraphEdge.id)  # static first: the cap drops the rest
        .limit(MAX_SYMBOL_EDGES * 2)
    ).all()

    other_ids = {
        i for e in rows for i in (e.src_symbol_id, e.dst_symbol_id) if i is not None
    } - set(own)
    others = (
        {s.id: s for s in db.scalars(select(Symbol).where(Symbol.id.in_(other_ids)))}
        if other_ids
        else {}
    )
    needed = {file.id}
    needed |= {sym.file_id for sym in others.values()}
    needed |= {e.src_file_id for e in rows if e.src_symbol_id is None and e.src_file_id is not None}
    all_paths = {f.id: f.path for f in db.scalars(select(File).where(File.id.in_(needed)))}

    nodes: dict[str, GraphNode] = {}
    in_deg: dict[str, int] = defaultdict(int)
    out_deg: dict[str, int] = defaultdict(int)

    def sym_node(s: Symbol) -> str:
        key = _sym_key(s.id)
        if key not in nodes:
            nodes[key] = GraphNode(
                id=key,
                kind="symbol",
                label=s.qualified_name.split("::", 1)[-1],
                path=all_paths.get(s.file_id),
                symbol_kind=s.kind,
                start_line=s.start_line,
                focus=s.file_id == file.id,
            )
        return key

    def file_node(file_id: int, p: str) -> str:
        key = _file_key(file_id)
        if key not in nodes:
            nodes[key] = GraphNode(
                id=key,
                kind="file",
                label="<top level>" if file_id == file.id else p,
                path=p,
                focus=file_id == file.id,
            )
        return key

    for s in own.values():
        sym_node(s)

    edges: list[GraphEdgeOut] = []
    ghost_counts: dict[str, int] = defaultdict(int)
    for e in rows:
        if e.resolution == "unresolved" and e.target_name:
            ghost_counts[e.target_name] += e.count
    ghosts = {
        name
        for name, _ in sorted(ghost_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_GHOSTS]
    }

    for e in rows:
        if len(edges) >= MAX_SYMBOL_EDGES:
            break
        if e.src_symbol_id is not None:
            sym = own.get(e.src_symbol_id) or others.get(e.src_symbol_id)
            if sym is None:
                continue
            src = sym_node(sym)
        else:
            owner_path = all_paths.get(e.src_file_id) if e.src_file_id is not None else None
            if owner_path is None or e.src_file_id is None:
                continue
            src = file_node(e.src_file_id, owner_path)
        if e.resolution == "static" and e.dst_symbol_id is not None:
            dsym = own.get(e.dst_symbol_id) or others.get(e.dst_symbol_id)
            if dsym is None:
                continue
            dst = sym_node(dsym)
        elif e.resolution == "unresolved" and e.target_name in ghosts:
            dst = f"ext:{e.target_name}"
            if dst not in nodes:
                nodes[dst] = GraphNode(id=dst, kind="external", label=e.target_name or "?")
        else:
            continue
        out_deg[src] += 1
        in_deg[dst] += 1
        edges.append(
            GraphEdgeOut(
                source=src,
                target=dst,
                kind="calls",
                resolution=e.resolution,
                count=e.count,
                line=e.line,
                reason=e.reason,
            )
        )
    for key, n in nodes.items():
        n.in_degree, n.out_degree = in_deg[key], out_deg[key]
    return GraphOut(
        snapshot=_ref(snapshot),
        level="symbol",
        nodes=list(nodes.values()),
        edges=edges,
        truncated=len(rows) > MAX_SYMBOL_EDGES * 2 - 1 or len(ghost_counts) > MAX_GHOSTS,
        total_nodes=len(nodes),
        note="Dashed edges are calls that could not be resolved to a definition.",
    )


# ------------------------------------------------------------------------------------ impact


def impact(
    db: Session,
    snapshot: Snapshot,
    file: str | None,
    symbol_id: int | None,
    depth: int,
) -> ImpactOut:
    require_built(snapshot)
    sid = snapshot.id
    depth = max(1, min(depth, MAX_DEPTH))

    target_file: File | None = None
    target_symbol: Symbol | None = None
    if symbol_id is not None:
        target_symbol = db.scalar(
            select(Symbol).where(Symbol.id == symbol_id, Symbol.snapshot_id == sid)
        )
        if target_symbol is None:
            raise SymbolNotFound("That symbol does not exist in this snapshot.")
        target_file = db.get(File, target_symbol.file_id)
    elif file is not None:
        target_file = db.scalar(select(File).where(File.snapshot_id == sid, File.path == file))
    if target_file is None:
        raise FileNotFound("That file does not exist in this snapshot.")

    graph: ReverseGraph = defaultdict(list)
    if target_symbol is not None:
        starts = [_sym_key(target_symbol.id)]
        for src_sym, src_file, dst_sym in db.execute(
            select(GraphEdge.src_symbol_id, GraphEdge.src_file_id, GraphEdge.dst_symbol_id).where(
                GraphEdge.snapshot_id == sid,
                GraphEdge.kind == "calls",
                GraphEdge.resolution == "static",
                GraphEdge.dst_symbol_id.is_not(None),
            )
        ):
            if dst_sym is None:
                continue
            if src_sym is not None:
                caller = _sym_key(src_sym)
            elif src_file is not None:
                caller = _file_key(src_file)
            else:
                continue
            graph[_sym_key(dst_sym)].append((caller, "calls"))
    else:
        starts = [_file_key(target_file.id)]
        for src, dst, kind, _c, _l in _file_edges(db, sid):
            graph[_file_key(dst)].append((_file_key(src), kind))

    reached, truncated = reverse_bfs(starts, graph, depth)

    sym_ids = {int(k[4:]) for k in [*reached, *starts] if k.startswith("sym:")}
    file_ids = {int(k[5:]) for k in [*reached, *starts] if k.startswith("file:")}
    syms = (
        {s.id: s for s in db.scalars(select(Symbol).where(Symbol.id.in_(sym_ids)))}
        if sym_ids
        else {}
    )
    file_ids |= {s.file_id for s in syms.values()}
    paths = (
        {f.id: f.path for f in db.scalars(select(File).where(File.id.in_(file_ids)))}
        if file_ids
        else {}
    )

    def describe(key: str) -> tuple[str, str | None]:
        if key.startswith("sym:"):
            s = syms[int(key[4:])]
            return paths[s.file_id], s.qualified_name.split("::", 1)[-1]
        return paths[int(key[5:])], None

    def label(key: str) -> str:
        p, name = describe(key)
        return f"{p}::{name}" if name else p

    dependents: list[Dependent] = []
    for key, r in sorted(reached.items(), key=lambda kv: (kv[1].depth, label(kv[0]))):
        p, name = describe(key)
        dependents.append(
            Dependent(
                path=p,
                symbol=name,
                depth=r.depth,
                hops=[
                    Hop(source=label(a), target=label(b), kind=k)
                    for a, b, k in path_to(key, reached, starts)
                ],
            )
        )

    depth_by_path: dict[str, int] = {}
    for d in dependents:
        depth_by_path[d.path] = min(d.depth, depth_by_path.get(d.path, 99))
    depth_by_path.pop(target_file.path, None)
    all_paths = list(
        db.scalars(select(File.path).where(File.snapshot_id == sid, File.language.is_not(None)))
    )
    tests = suggest_tests([target_file.path], depth_by_path, all_paths)

    target = ImpactTarget(
        type="symbol" if target_symbol else "file",
        path=target_file.path,
        symbol=target_symbol.qualified_name.split("::", 1)[-1] if target_symbol else None,
        symbol_id=target_symbol.id if target_symbol else None,
    )
    limitations = list(LIMITATIONS)
    if target_symbol is not None:
        limitations.append(
            "For a symbol, only its callers are followed. Code that imports it without calling "
            "it (passing it around, type annotations, re-exports) appears only in the file-level "
            "view."
        )
    return ImpactOut(
        snapshot=_ref(snapshot),
        target=target,
        max_depth=depth,
        dependents=dependents,
        direct=sum(1 for d in dependents if d.depth == 1),
        transitive=sum(1 for d in dependents if d.depth > 1),
        files_affected=len({d.path for d in dependents} - {target_file.path}),
        truncated=truncated,
        unverified_callers=_unverified(db, sid, target_file, target_symbol),
        suggested_tests=[
            SuggestedTestOut(
                path=t.path,
                basis=t.basis,
                confidence=t.confidence,
                reason=t.reason,
                heuristic=t.heuristic,
            )
            for t in tests
        ],
        limitations=limitations,
    )


def _unverified(
    db: Session, sid: Any, target_file: File, target_symbol: Symbol | None
) -> list[UnverifiedCaller]:
    """Unresolved calls whose last name segment equals a changed name. Name match only: these
    are leads to check by hand, never dependents."""
    if target_symbol is not None:
        names = [target_symbol.name]
    else:
        names = list(
            db.scalars(
                select(Symbol.name)
                .where(Symbol.file_id == target_file.id, Symbol.parent_id.is_(None))
                .distinct()
                .limit(30)
            )
        )
    names = [n for n in names if len(n) >= 4]
    if not names:
        return []
    conds = [GraphEdge.target_name == n for n in names] + [
        GraphEdge.target_name.like(f"%.{n}") for n in names
    ]
    rows = db.execute(
        select(GraphEdge, File.path)
        .join(File, File.id == GraphEdge.src_file_id)
        .where(
            GraphEdge.snapshot_id == sid,
            GraphEdge.kind == "calls",
            GraphEdge.resolution == "unresolved",
            or_(*conds),
        )
        .order_by(File.path, GraphEdge.line)
        .limit(MAX_UNVERIFIED * 3)
    ).all()
    sym_ids = {e.src_symbol_id for e, _p in rows if e.src_symbol_id is not None}
    qn = (
        {
            s.id: s.qualified_name.split("::", 1)[-1]
            for s in db.scalars(select(Symbol).where(Symbol.id.in_(sym_ids)))
        }
        if sym_ids
        else {}
    )
    out = []
    for e, path in rows:
        last = (e.target_name or "").rsplit(".", 1)[-1]
        if last not in names:
            continue
        out.append(
            UnverifiedCaller(
                path=path,
                symbol=qn.get(e.src_symbol_id) if e.src_symbol_id is not None else None,
                line=e.line,
                called_as=e.target_name or "",
                reason=e.reason,
            )
        )
        if len(out) >= MAX_UNVERIFIED:
            break
    return out
