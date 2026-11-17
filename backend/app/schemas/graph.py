from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.files import SnapshotRef


class GraphNode(BaseModel):
    id: str = Field(description="Stable key: a file path, or `path::qualified.name` for a symbol.")
    kind: Literal["file", "symbol", "external"]
    label: str
    path: str | None = None
    language: str | None = None
    symbol_kind: str | None = None
    start_line: int | None = None
    in_degree: int = 0
    out_degree: int = 0
    focus: bool = False


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    kind: Literal["imports", "calls"]
    resolution: Literal["static", "unresolved"]
    count: int = 1
    line: int | None = None
    reason: str | None = None


class GraphOut(BaseModel):
    snapshot: SnapshotRef
    level: Literal["file", "symbol"]
    nodes: list[GraphNode]
    edges: list[GraphEdgeOut]
    truncated: bool = Field(description="True when nodes or edges were cut to keep this readable.")
    total_nodes: int
    note: str | None = None


class ImpactTarget(BaseModel):
    type: Literal["file", "symbol"]
    path: str
    symbol: str | None = None
    symbol_id: int | None = None


class Hop(BaseModel):
    source: str
    target: str
    kind: Literal["imports", "calls"]


class Dependent(BaseModel):
    path: str
    symbol: str | None = None
    depth: int
    hops: list[Hop] = Field(description="Edges from the changed code to this dependent.")


class UnverifiedCaller(BaseModel):
    path: str
    symbol: str | None
    line: int | None
    called_as: str
    reason: str | None


class SuggestedTestOut(BaseModel):
    path: str
    basis: Literal["static_dependency", "naming_convention"]
    confidence: Literal["high", "medium", "low"]
    reason: str
    heuristic: bool


class ImpactOut(BaseModel):
    snapshot: SnapshotRef
    target: ImpactTarget
    max_depth: int
    dependents: list[Dependent] = Field(description="Static facts only, nearest first.")
    direct: int
    transitive: int
    files_affected: int
    truncated: bool
    unverified_callers: list[UnverifiedCaller] = Field(
        description="Calls that use the changed name but could not be resolved to it. Matched by "
        "name only; NOT facts."
    )
    suggested_tests: list[SuggestedTestOut]
    limitations: list[str]


class ReviewRequest(BaseModel):
    file: str | None = Field(default=None, max_length=500)
    symbol: int | None = Field(default=None, ge=1)
    depth: int = Field(default=3, ge=1, le=6)


class AiRisk(BaseModel):
    description: str
    confidence: Literal["low", "medium", "high"]
    paths: list[str] = Field(
        description="Files this risk is about; each is one the static analysis listed."
    )


class AiTestIdea(BaseModel):
    path: str
    reason: str


class AiReviewOut(BaseModel):
    notice: str
    model: str | None
    summary: str
    risks: list[AiRisk]
    tests_to_consider: list[AiTestIdea]
    open_questions: list[str]
    dropped: int = Field(description="Items removed because they cited files that are not real.")
    static_dependents_considered: int
