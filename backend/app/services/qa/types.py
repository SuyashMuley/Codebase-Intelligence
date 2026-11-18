"""Plain data shared by the question-answering steps. No database or network types."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Excerpt:
    """One piece of code shown to the model, under a short per-request id like `C3`."""

    alias: str
    chunk_id: int
    path: str
    start_line: int  # 1-based, inclusive; the chunk's own range
    end_line: int
    text: str  # exactly the lines start_line..end_line
    symbol: str | None = None
    via: str = "search"  # "search" or "graph" (a neighbour of a search hit)


@dataclass(frozen=True)
class Located:
    """Where in the file a quoted claim is supported: the quote's own lines."""

    alias: str
    chunk_id: int
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ValidClaim:
    text: str
    support: list[Located]  # one entry per cited excerpt that really contains the quote


@dataclass
class Validation:
    claims: list[ValidClaim] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)  # why claims were rejected, for a retry
    dropped: int = 0
    cannot_determine: bool = False
    missing: str = ""


@dataclass(frozen=True)
class Citation:
    n: int  # the number shown next to the claim
    chunk_id: int
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class AnswerClaim:
    text: str
    cites: list[int]  # Citation.n values


@dataclass
class AnswerResult:
    claims: list[AnswerClaim] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    insufficient_evidence: bool = False
    reason: str | None = None  # why there is no answer: no_relevant_code, model_declined, ...
    confidence: str = "none"  # none | low | medium | high
    missing: str = ""
    dropped_claims: int = 0
    notes: list[str] = field(default_factory=list)
    model: str | None = None
    retrieval_mode: str = "vector"
    chunks_considered: int = 0
    expanded: int = 0
    top_similarity: float | None = None
    attempts: int = 0
    trace: list[str] = field(default_factory=list)
