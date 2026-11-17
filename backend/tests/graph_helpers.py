"""Build a graph from in-memory source files using the real parsers, with no database."""

import re
from dataclasses import dataclass

from app.services.github.filters import LANGUAGE_BY_EXT
from app.services.graph.builder import GraphBuilder
from app.services.graph.model import Edge, FileInfo, GraphResult, RefInfo, SymInfo
from app.services.parsing.parse import parse_source


@dataclass
class Built:
    result: GraphResult
    files: dict[str, int]  # path -> id
    symbols: dict[str, int]  # "<path>::<qualified>" -> id

    def path(self, file_id: int | None) -> str | None:
        return next((p for p, i in self.files.items() if i == file_id), None)

    def sym(self, symbol_id: int | None) -> str | None:
        return next((k for k, i in self.symbols.items() if i == symbol_id), None)

    def imports(self, static: bool = True) -> set[tuple[str, str | None]]:
        want = "static" if static else "unresolved"
        return {
            (self.path(e.src_file_id) or "?", self.path(e.dst_file_id) or e.target_name)
            for e in self.result.edges
            if e.kind == "imports" and e.resolution == want
        }

    def calls(self) -> set[tuple[str | None, str | None]]:
        """Static call edges as (caller, callee); a module-level caller is shown by its path."""
        return {
            (self.sym(e.src_symbol_id) or self.path(e.src_file_id), self.sym(e.dst_symbol_id))
            for e in self.result.edges
            if e.kind == "calls" and e.resolution == "static"
        }

    def unresolved_calls(self) -> list[Edge]:
        return [e for e in self.result.edges if e.kind == "calls" and e.resolution == "unresolved"]

    def reasons(self) -> dict[str, str | None]:
        return {
            e.target_name or "": e.reason for e in self.result.edges if e.resolution == "unresolved"
        }


def build_graph(sources: dict[str, str], repo: str = "o/r") -> Built:
    files: list[FileInfo] = []
    symbols: list[SymInfo] = []
    refs: list[RefInfo] = []
    file_ids: dict[str, int] = {}
    symbol_ids: dict[str, int] = {}
    next_symbol = 1
    for file_id, (path, content) in enumerate(sources.items(), start=1):
        ext = "." + path.rsplit(".", 1)[-1]
        language = LANGUAGE_BY_EXT[ext]
        match = re.search(r"(?m)^package\s+(\w+)", content) if language == "go" else None
        files.append(FileInfo(file_id, path, language, match.group(1) if match else None))
        file_ids[path] = file_id
        parsed = parse_source(path, language, content).parsed
        index_to_id: dict[int, int] = {}
        for i, s in enumerate(parsed.symbols):
            index_to_id[i] = next_symbol
            symbol_ids[s.qualified_name] = next_symbol
            next_symbol += 1
        for i, s in enumerate(parsed.symbols):
            symbols.append(
                SymInfo(
                    index_to_id[i],
                    file_id,
                    s.kind,
                    s.name,
                    index_to_id[s.parent] if s.parent is not None else None,
                    s.signature,
                )
            )
        for r in parsed.refs:
            refs.append(
                RefInfo(
                    file_id,
                    index_to_id[r.symbol] if r.symbol is not None else None,
                    r.kind,
                    r.name,
                    r.line,
                    r.detail,
                )
            )
    result = GraphBuilder(files, symbols, refs, repo).build()
    return Built(result, file_ids, symbol_ids)
