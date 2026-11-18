import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RepositoryCreate(BaseModel):
    url: str = Field(
        min_length=1,
        max_length=200,
        examples=["https://github.com/pallets/flask"],
        description=(
            "GitHub repository URL. Whether it is private is read from GitHub with your own "
            "token, never taken from this request."
        ),
    )


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner: str
    name: str
    url: str
    default_branch: str | None
    created_at: datetime
    is_private: bool = False


class RepositoryList(BaseModel):
    items: list[RepositoryOut]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, object]
    request_id: str | None


class ErrorEnvelope(BaseModel):
    error: ErrorBody
