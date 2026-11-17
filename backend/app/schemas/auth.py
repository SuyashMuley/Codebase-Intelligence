import uuid

from pydantic import BaseModel, ConfigDict


class MeOut(BaseModel):
    """The signed-in user. Deliberately minimal: no tokens, no email."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    login: str
    avatar_url: str | None
