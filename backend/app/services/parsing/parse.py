"""Parse one source file into symbols, references and chunks."""

import time
from dataclasses import dataclass
from functools import lru_cache

import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser

from app.services.parsing import go, javascript, python
from app.services.parsing.chunker import Chunk, build_chunks
from app.services.parsing.model import ParsedFile

PARSE_TIMEOUT_SECONDS = 5.0
_READ_SIZE = 64 * 1024


@lru_cache
def _language(name: str) -> Language:
    return Language(
        {
            "python": tree_sitter_python.language,
            "javascript": tree_sitter_javascript.language,
            "typescript": tree_sitter_typescript.language_typescript,
            "tsx": tree_sitter_typescript.language_tsx,
            "go": tree_sitter_go.language,
        }[name]()
    )


def grammar_for(path: str, language: str) -> str:
    """Which grammar to use. TSX needs its own; the JavaScript grammar already handles JSX."""
    if language == "typescript" and path.lower().endswith(".tsx"):
        return "tsx"
    return language


@dataclass
class ParsedSource:
    parsed: ParsedFile
    chunks: list[Chunk]
    line_count: int


def parse_source(
    path: str,
    language: str,
    content: str,
    max_chars: int = 1800,
    merge_below: int = 500,
) -> ParsedSource:
    """Never raises on bad input: unparseable or pathological files yield partial results with
    `syntax_errors` or `truncated` set, and are still chunked by plain line windows."""
    lines = content.split("\n")
    if language == "markdown":  # prose: no grammar, plain line-window chunks
        return ParsedSource(
            parsed=ParsedFile(),
            chunks=build_chunks(lines, [], max_chars, merge_below),
            line_count=len(lines),
        )
    src = content.encode("utf-8")
    grammar = grammar_for(path, language)

    started = time.monotonic()

    def read(offset: int, _point: object) -> bytes:
        if time.monotonic() - started > PARSE_TIMEOUT_SECONDS:
            return b""  # an empty read ends the input, so a runaway parse stops
        return src[offset : offset + _READ_SIZE]

    parser = Parser(_language(grammar))
    tree = parser.parse(read)
    if tree is None:  # pragma: no cover - only if the parser has no language
        parsed = ParsedFile(syntax_errors=True, truncated=True)
    else:
        extract = {"python": python, "go": go}.get(language, javascript).extract
        parsed = extract(tree.root_node, src, lines, path)
        if time.monotonic() - started > PARSE_TIMEOUT_SECONDS:
            parsed.truncated = True

    chunks = build_chunks(lines, parsed.symbols, max_chars, merge_below)
    return ParsedSource(parsed=parsed, chunks=chunks, line_count=len(lines))
