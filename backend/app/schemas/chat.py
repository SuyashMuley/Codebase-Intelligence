from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.files import SnapshotRef


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=1500)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[ChatTurn] = Field(
        default_factory=list,
        max_length=6,
        description="Earlier turns of this conversation, oldest first. Context only; nothing in "
        "it is trusted as evidence.",
    )
    snapshot: str | None = Field(
        default=None, description="A specific snapshot of this repository. Defaults to the newest."
    )


class CitationOut(BaseModel):
    n: int = Field(description="The number shown next to the claims that use it.")
    chunk_id: int
    path: str
    start_line: int
    end_line: int


class ClaimOut(BaseModel):
    text: str
    cites: list[int] = Field(description="Numbers of the citations that support this claim.")


class RetrievalInfo(BaseModel):
    mode: str
    chunks_considered: int
    expanded_from_graph: int
    top_similarity: float | None


class ChatAnswer(BaseModel):
    insufficient_evidence: bool = Field(
        description="True when the question could not be answered from the indexed code."
    )
    reason: str | None = Field(
        default=None, description="Why there is no answer, when insufficient_evidence is true."
    )
    confidence: Literal["none", "low", "medium", "high"] = Field(
        description="A label, not a probability: high means strong retrieval and every claim "
        "verified on the first attempt; none means no answer."
    )
    claims: list[ClaimOut]
    citations: list[CitationOut]
    missing: str = Field(description="What the retrieved code does not cover, if anything.")
    notes: list[str]
    dropped_claims: int
    model: str | None
    snapshot: SnapshotRef
    retrieval: RetrievalInfo
    attempts: int
    trace: list[str] = Field(description="The workflow steps that ran, in order.")
