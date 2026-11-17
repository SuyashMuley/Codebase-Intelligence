"""What gets embedded for a chunk. Shared by the indexer and the benchmark so they cannot drift."""


def document_text(path: str, text: str) -> str:
    """The chunk's own text, headed by its file path (paths carry a lot of meaning in code).
    The stored chunk text itself stays exactly the cited lines."""
    return f"File: {path}\n{text}"
