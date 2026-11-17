"""Change-impact analysis over the static graph. Pure functions: no database, no network.

Two kinds of answer are kept apart on purpose:
- *dependents* are facts: found by walking static edges (imports and resolved calls) backwards
  from the changed code. Every dependent comes with the edge path that reaches it.
- *suggested tests* mix facts (a test that depends on the change, found the same way) with a
  naming-convention heuristic, and each suggestion says which one it is.
"""

import posixpath
import re
from collections import deque
from dataclasses import dataclass

# node key -> [(dependent key, edge kind)]: who depends on this node
ReverseGraph = dict[str, list[tuple[str, str]]]


@dataclass(frozen=True)
class Reach:
    depth: int
    parent: str
    via: str  # kind of the edge between parent and this node


def reverse_bfs(
    starts: list[str], graph: ReverseGraph, max_depth: int, max_nodes: int = 2000
) -> tuple[dict[str, Reach], bool]:
    """Breadth-first walk to dependents. Returns the nodes reached with the shortest path back
    to a start, and whether `max_nodes` cut the walk short. Cycles are handled by the visited
    set: a node is reached once, at its shortest depth."""
    reached: dict[str, Reach] = {}
    seen = set(starts)
    queue: deque[tuple[str, int]] = deque((s, 0) for s in starts)
    truncated = False
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for dependent, kind in sorted(graph.get(node, [])):
            if dependent in seen:
                continue
            if len(reached) >= max_nodes:
                truncated = True
                continue
            seen.add(dependent)
            reached[dependent] = Reach(depth + 1, node, kind)
            queue.append((dependent, depth + 1))
    return reached, truncated


def path_to(node: str, reached: dict[str, Reach], starts: list[str]) -> list[tuple[str, str, str]]:
    """The hops from a changed node to `node`, as (from, to, edge kind), nearest the change
    first."""
    hops: list[tuple[str, str, str]] = []
    cur = node
    while cur in reached:
        r = reached[cur]
        hops.append((r.parent, cur, r.via))
        cur = r.parent
        if len(hops) > len(reached) + 1:  # defensive: parent links never loop, but never hang
            break
    hops.reverse()
    return hops


# --- tests ------------------------------------------------------------------------------------

_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})


def is_test_path(path: str) -> bool:
    name = posixpath.basename(path)
    parts = path.split("/")[:-1]
    if name.endswith("_test.go"):
        return True
    if re.match(r"^test_.+\.py$", name) or re.match(r".+_test\.py$", name):
        return True
    if re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name):
        return True
    return any(p in _TEST_DIRS for p in parts) and name not in ("conftest.py", "__init__.py")


def _stem(path: str) -> str:
    name = posixpath.basename(path)
    name = re.sub(r"\.(test|spec)(?=\.)", "", name)
    stem = posixpath.splitext(name)[0]
    stem = re.sub(r"^test_", "", stem)
    return re.sub(r"_(test|spec)$", "", stem)


@dataclass
class SuggestedTest:
    path: str
    basis: str  # "static_dependency" | "naming_convention"
    confidence: str  # high | medium | low
    reason: str
    heuristic: bool
    depth: int | None = None


def suggest_tests(
    changed_paths: list[str],
    dependent_depths: dict[str, int],
    all_paths: list[str],
    limit: int = 30,
) -> list[SuggestedTest]:
    """Tests worth running for a change.

    1. Test files that statically depend on the changed code (fact; closer is stronger).
    2. Test files whose name follows the convention for a changed file (heuristic).
    """
    out: dict[str, SuggestedTest] = {}
    for path, depth in dependent_depths.items():
        if not is_test_path(path):
            continue
        out[path] = SuggestedTest(
            path=path,
            basis="static_dependency",
            confidence="high" if depth == 1 else "medium",
            reason=(
                "imports or calls the changed code directly"
                if depth == 1
                else f"depends on the changed code through {depth} steps"
            ),
            heuristic=False,
            depth=depth,
        )
    wanted = {_stem(p) for p in changed_paths if not is_test_path(p)}
    wanted.discard("")
    wanted.discard("__init__")
    if wanted:
        for path in all_paths:
            if path in out or not is_test_path(path):
                continue
            if _stem(path) in wanted:
                out[path] = SuggestedTest(
                    path=path,
                    basis="naming_convention",
                    confidence="low",
                    reason="test file named after a changed file; no static link was found",
                    heuristic=True,
                )
    order = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(out.values(), key=lambda s: (order[s.confidence], s.depth or 99, s.path))
    return ranked[:limit]
