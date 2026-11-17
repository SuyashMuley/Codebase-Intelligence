"""Plain data for the graph builder. No database types, so the builder is a pure function."""

from dataclasses import dataclass, field
from typing import Any

STATIC, UNRESOLVED = "static", "unresolved"
IMPORTS, CALLS = "imports", "calls"


@dataclass(frozen=True)
class FileInfo:
    id: int
    path: str
    language: str | None
    go_package: str | None = None  # the `package x` clause, Go only


@dataclass(frozen=True)
class SymInfo:
    id: int
    file_id: int
    kind: str
    name: str
    parent_id: int | None
    # First line of the declaration. Tells a declaration (`function f`) from a member assignment
    # (`req.accepts = function`), which JavaScript extraction also records as a symbol.
    signature: str | None = None


@dataclass(frozen=True)
class RefInfo:
    file_id: int
    symbol_id: int | None  # the enclosing symbol; None at module level
    kind: str  # "import" | "call"
    name: str
    line: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    kind: str  # imports | calls
    resolution: str  # static | unresolved
    src_file_id: int
    dst_file_id: int | None = None
    src_symbol_id: int | None = None
    dst_symbol_id: int | None = None
    target_name: str | None = None  # what the code said, kept for unresolved edges
    reason: str | None = None  # why it is unresolved
    line: int | None = None
    count: int = 1


@dataclass
class GraphResult:
    edges: list[Edge] = field(default_factory=list)
    truncated: bool = False
