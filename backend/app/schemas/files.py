import uuid

from pydantic import BaseModel


class SnapshotRef(BaseModel):
    id: uuid.UUID
    commit_sha: str
    state: str


class FileEntry(BaseModel):
    path: str
    language: str | None
    size_bytes: int
    skipped_reason: str | None
    redactions: int
    parse_status: str | None
    symbol_count: int


class FileList(BaseModel):
    snapshot: SnapshotRef
    items: list[FileEntry]
    truncated: bool


class SymbolOut(BaseModel):
    id: int
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    parent_id: int | None
    signature: str | None


class FileDetail(BaseModel):
    snapshot: SnapshotRef
    path: str
    language: str | None
    size_bytes: int
    skipped_reason: str | None
    redactions: int
    parse_status: str | None
    line_count: int
    # None when the file was skipped (too large, binary, ...). Secret-looking values in it have
    # already been replaced by [REDACTED:...] placeholders.
    content: str | None
    symbols: list[SymbolOut]


class Counts(BaseModel):
    total: int
    by_kind: dict[str, int]


class FileStats(BaseModel):
    stored: int  # rows in the explorer: parsed source plus notable skips
    parsed: int
    bytes: int
    by_language: dict[str, int]
    skipped: dict[str, int]  # every skipped file in the repository, by reason


class ChunkStats(BaseModel):
    total: int
    embedded: int
    embedding_model: str | None
    average_chars: int
    largest_chars: int


class RefStats(BaseModel):
    imports: int
    calls: int


class GraphStats(BaseModel):
    static_imports: int
    unresolved_imports: int
    static_calls: int
    unresolved_calls: int
    truncated: bool


class RedactionStats(BaseModel):
    spans: int
    files: int


class StatsOut(BaseModel):
    snapshot: SnapshotRef
    parsing_done: bool
    files: FileStats
    symbols: Counts
    chunks: ChunkStats
    refs: RefStats
    graph: GraphStats | None  # None until the graph phase has run
    redactions: RedactionStats
    syntax_error_files: int
