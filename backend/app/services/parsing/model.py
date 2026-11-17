"""Plain data produced by the language extractors. No database or Tree-sitter types here."""

from dataclasses import dataclass, field
from typing import Any

# Symbol kinds used across languages.
FUNCTION, METHOD, CLASS = "function", "method", "class"
INTERFACE, TYPE, ENUM, STRUCT, NAMESPACE = "interface", "type", "enum", "struct", "namespace"

IMPORT, CALL = "import", "call"


@dataclass(frozen=True)
class Symbol:
    kind: str
    name: str
    qualified_name: str  # "<path>::Outer.name"
    start_line: int  # 1-based, inclusive; includes decorators and leading comments
    end_line: int
    parent: int | None  # index of the enclosing symbol in the same list, if nested
    signature: str  # first line of the declaration, trimmed


@dataclass
class Ref:
    """A statically visible reference. Imports are resolved to files later (graph milestone);
    calls are only recorded by name and receiver, never guessed to a target."""

    kind: str  # "import" | "call"
    name: str  # module specifier as written, or callee name
    line: int
    symbol: int | None  # index of the enclosing symbol; None at module level
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedFile:
    symbols: list[Symbol] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    syntax_errors: bool = False  # Tree-sitter recovered from errors; results may be partial
    truncated: bool = False  # hit a safety limit (node budget) and stopped early
