"""Run the parsers over this repository's own source.

Real code is the best fixture: it exercises syntax no hand-written sample thinks of. Python is
also checked against the standard library's `ast` module, an independent implementation, so a
bug in our extraction cannot hide behind our own expectations.
"""

import ast
from pathlib import Path

import pytest

from app.services.parsing.parse import parse_source
from tests.parse_helpers import check_chunks, check_symbols

REPO = Path(__file__).resolve().parents[3]


def project_files(root: str, *suffixes: str) -> list[Path]:
    base = REPO / root
    skip = {"node_modules", ".venv", ".next", "__pycache__"}
    return sorted(
        p
        for p in base.rglob("*")
        if p.suffix in suffixes and p.is_file() and not skip & set(p.relative_to(REPO).parts)
    )


PY_FILES = project_files("backend", ".py")
TS_FILES = project_files("frontend/src", ".ts", ".tsx")


def test_the_project_has_enough_real_files_to_be_a_meaningful_check() -> None:
    assert len(PY_FILES) >= 40 and len(TS_FILES) >= 15


def _scope_defs(body: list[ast.stmt], prefix: str, in_class: bool) -> list[tuple[str, str, int]]:
    """Definitions at module/class scope, descending into compound statements but not into
    function bodies: the same scoping rules our extractor uses."""
    out: list[tuple[str, str, int]] = []
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            assert node.end_lineno is not None
            out.append(("method" if in_class else "function", prefix + node.name, node.end_lineno))
        elif isinstance(node, ast.ClassDef):
            assert node.end_lineno is not None
            out.append(("class", prefix + node.name, node.end_lineno))
            out += _scope_defs(node.body, f"{prefix}{node.name}.", True)
        elif isinstance(
            node, ast.If | ast.While | ast.For | ast.AsyncFor | ast.With | ast.AsyncWith
        ):
            out += _scope_defs(node.body, prefix, in_class)
            out += _scope_defs(getattr(node, "orelse", []), prefix, in_class)
        elif isinstance(node, ast.Try | ast.TryStar):
            for block in (node.body, node.orelse, node.finalbody):
                out += _scope_defs(block, prefix, in_class)
            for handler in node.handlers:
                out += _scope_defs(handler.body, prefix, in_class)
    return out


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_python_matches_the_standard_library_parser(path: Path) -> None:
    content = path.read_text()
    rel = str(path.relative_to(REPO))
    result = parse_source(rel, "python", content)
    lines = content.split("\n")
    check_symbols(result.parsed, len(lines))
    check_chunks(content, result.chunks)
    assert result.parsed.syntax_errors is False, "valid Python must parse without error nodes"
    assert result.parsed.truncated is False

    ours: dict[tuple[str, str], list[int]] = {}
    for sym in result.parsed.symbols:
        key = (sym.kind, sym.qualified_name.split("::", 1)[1])
        ours.setdefault(key, []).append(sym.end_line)
    theirs: dict[tuple[str, str], list[int]] = {}
    for kind, name, end in _scope_defs(ast.parse(content).body, "", False):
        theirs.setdefault((kind, name), []).append(end)

    # Exactly the same symbols (kind and qualified name, counting duplicates like property
    # setters). Tree-sitter keeps an indented trailing comment inside the function it follows,
    # where `ast` stops at the last statement, so an end line may be later but only over
    # comments and blank lines.
    assert {k: len(v) for k, v in ours.items()} == {k: len(v) for k, v in theirs.items()}
    for key, ast_ends in theirs.items():
        for ast_end, our_end in zip(sorted(ast_ends), sorted(ours[key]), strict=True):
            assert our_end >= ast_end, (key, ast_end, our_end)
            for extra in lines[ast_end:our_end]:
                assert not extra.strip() or extra.strip().startswith("#"), (key, extra)

    # Each symbol really starts at (or just above) its own def/class keyword.
    for s in result.parsed.symbols:
        assert s.name in s.signature, (s.name, s.signature)
        text = "\n".join(lines[s.start_line - 1 : s.end_line])
        assert s.name in text


@pytest.mark.parametrize("path", TS_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_typescript_parses_cleanly_with_consistent_ranges(path: Path) -> None:
    content = path.read_text()
    rel = str(path.relative_to(REPO))
    result = parse_source(rel, "typescript", content)
    check_symbols(result.parsed, len(content.split("\n")))
    check_chunks(content, result.chunks)
    assert result.parsed.syntax_errors is False, "valid TypeScript/TSX must parse without errors"
    lines = content.split("\n")
    for s in result.parsed.symbols:
        if s.name != "default":
            assert s.name in "\n".join(lines[s.start_line - 1 : s.end_line]), s


def test_every_exported_function_in_the_frontend_is_found() -> None:
    """A direct cross-check on real components: each `export function Name` is a symbol."""
    import re

    missing = []
    for path in TS_FILES:
        content = path.read_text()
        result = parse_source(str(path), "typescript", content)
        found = {s.name for s in result.parsed.symbols}
        for name in re.findall(r"^export (?:async )?function (\w+)", content, re.MULTILINE):
            if name not in found:
                missing.append((path.name, name))
    assert missing == []


def test_every_import_in_the_frontend_is_found() -> None:
    import re

    problems = []
    for path in TS_FILES:
        content = path.read_text()
        result = parse_source(str(path), "typescript", content)
        found = {r.name for r in result.parsed.refs if r.kind == "import"}
        for spec in re.findall(r'^import [^;]*? from "([^"]+)"', content, re.MULTILINE | re.DOTALL):
            if spec not in found:
                problems.append((path.name, spec))
    assert problems == []
