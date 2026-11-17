"""Shared assertions for parsing and chunking tests."""

from collections.abc import Sequence

from app.services.parsing.chunker import Chunk
from app.services.parsing.model import ParsedFile, Ref, Symbol
from app.services.parsing.parse import ParsedSource, parse_source


def symbols(parsed: ParsedFile) -> list[tuple[str, str, int, int]]:
    """(kind, name, start_line, end_line) for each symbol."""
    return [(s.kind, s.name, s.start_line, s.end_line) for s in parsed.symbols]


def qualified(parsed: ParsedFile) -> list[str]:
    return [s.qualified_name.split("::", 1)[1] for s in parsed.symbols]


def imports(parsed: ParsedFile) -> list[Ref]:
    return [r for r in parsed.refs if r.kind == "import"]


def calls(parsed: ParsedFile) -> list[tuple[str, str | None, str | None]]:
    """(callee, receiver, enclosing symbol's qualified name) for each distinct call."""
    out = []
    for r in parsed.refs:
        if r.kind == "call":
            owner = (
                parsed.symbols[r.symbol].qualified_name.split("::", 1)[1]
                if r.symbol is not None
                else None
            )
            out.append((r.name, r.detail.get("receiver"), owner))
    return out


def check_symbols(parsed: ParsedFile, line_count: int) -> None:
    for i, s in enumerate(parsed.symbols):
        assert 1 <= s.start_line <= s.end_line <= line_count, (s, line_count)
        if s.parent is not None:
            assert s.parent < i, "a parent is listed before its children"
            p = parsed.symbols[s.parent]
            assert p.start_line <= s.start_line and s.end_line <= p.end_line, (s, p)


def check_chunks(content: str, chunks: Sequence[Chunk], max_chars: int = 1800) -> None:
    """The chunker's guarantees: exact, exactly-once coverage of non-blank lines, sane sizes."""
    lines = content.split("\n")
    previous_end = 0
    covered: set[int] = set()
    for c in sorted(chunks, key=lambda c: c.start_line):
        assert 1 <= c.start_line <= c.end_line <= len(lines), c
        assert c.start_line > previous_end, f"chunks overlap at line {c.start_line}"
        assert lines[c.start_line - 1].strip(), f"chunk starts on a blank line: {c}"
        assert lines[c.end_line - 1].strip(), f"chunk ends on a blank line: {c}"
        text = "\n".join(lines[c.start_line - 1 : c.end_line])
        assert len(text) <= max_chars or c.start_line == c.end_line, f"oversized chunk {c}"
        covered.update(range(c.start_line, c.end_line + 1))
        previous_end = c.end_line
    non_blank = {i + 1 for i, line in enumerate(lines) if line.strip()}
    assert non_blank <= covered, f"uncovered lines: {sorted(non_blank - covered)[:10]}"


def parse_ok(path: str, language: str, content: str, **kw: int) -> ParsedSource:
    result = parse_source(path, language, content, **kw)
    check_symbols(result.parsed, result.line_count)
    check_chunks(content, result.chunks, kw.get("max_chars", 1800))
    return result


def sym_by_name(parsed: ParsedFile, name: str) -> Symbol:
    matches = [s for s in parsed.symbols if s.name == name]
    assert len(matches) == 1, f"expected one symbol named {name}, found {len(matches)}"
    return matches[0]
