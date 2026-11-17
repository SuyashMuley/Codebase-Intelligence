"""Helpers shared by the language extractors."""

from tree_sitter import Node

SIGNATURE_MAX = 200
MAX_NODES = 400_000  # per file; a safety net against pathological input
MAX_DEPTH = 60  # declaration nesting we are willing to follow
MAX_CALLS_PER_FILE = 5_000  # after de-duplication


def line_range(node: Node) -> tuple[int, int]:
    """1-based inclusive line span. A node that ends at column 0 does not include that line."""
    start = node.start_point[0] + 1
    end_row, end_col = node.end_point
    end = end_row + 1 if end_col > 0 else end_row
    return start, max(end, start)


def node_text(node: Node | None, src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def signature_of(lines: list[str], start_line: int) -> str:
    text = lines[start_line - 1].strip() if 0 < start_line <= len(lines) else ""
    return text[:SIGNATURE_MAX]


def attach_leading_comments(
    lines: list[str], start: int, floor: int, prefixes: tuple[str, ...]
) -> int:
    """Move `start` up over comment lines directly above it (no blank line between), so a
    symbol's chunk carries its doc comment. Never goes above `floor` (the parent's body)."""
    line = start
    while line - 1 >= max(floor, 1):
        above = lines[line - 2].strip()
        if not above or not above.startswith(prefixes):
            break
        line -= 1
    return line
