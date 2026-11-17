import uuid
from typing import Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="A question or identifiers.")
    mode: Literal["hybrid", "keyword", "vector"] = "vector"
    limit: int = Field(default=10, ge=1, le=20)
    snapshot: uuid.UUID | None = Field(
        default=None, description="A specific snapshot of this repository. Defaults to the newest."
    )


class SearchHitOut(BaseModel):
    chunk_id: int
    path: str
    start_line: int
    end_line: int
    symbol: str | None
    text: str
    score: float
    keyword_rank: int | None
    vector_rank: int | None


class SearchOut(BaseModel):
    mode_used: Literal["hybrid", "keyword", "vector"]
    # e.g. "embeddings_not_ready", "embedding_failed:embedding_unavailable": why a hybrid or
    # vector search was answered by keyword search alone.
    warnings: list[str]
    hits: list[SearchHitOut]
