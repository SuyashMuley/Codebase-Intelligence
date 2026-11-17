import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class IndexRequest(BaseModel):
    ref: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[A-Za-z0-9._/-]+$",
        description="Branch, tag or commit SHA. Defaults to the repository's default branch.",
    )

    @field_validator("ref")
    @classmethod
    def _no_traversal(cls, v: str | None) -> str | None:
        if v is not None and (".." in v or v.startswith("/") or v.endswith("/")):
            raise ValueError("invalid ref")
        return v


class FetchProgress(BaseModel):
    files_kept: int
    bytes_kept: int
    skipped: dict[str, int]


class JobError(BaseModel):
    code: str
    message: str


class StatusOut(BaseModel):
    state: Literal["not_indexed", "pending", "running", "ready", "failed"]
    snapshot_id: uuid.UUID | None = None
    commit_sha: str | None = None
    phase: str | None = None
    error: JobError | None = None
    attempts: int
    max_attempts: int
    retry_at: datetime | None = Field(
        default=None, description="Set while the job is backing off after a transient error."
    )
    can_resume: bool = Field(
        default=False, description="True when the job failed; POST /index restarts it."
    )
    progress: FetchProgress | None = None
    updated_at: datetime | None = None


class IndexStartOut(BaseModel):
    created: bool = Field(description="False when this commit already had a snapshot.")
    status: StatusOut


class TickOut(BaseModel):
    ran: bool
    reason: str | None = None
    status: StatusOut
