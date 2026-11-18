from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class ProviderIn(BaseModel):
    kind: str = Field(min_length=1, max_length=30, examples=["openai"])
    mode: Literal["stored", "browser"] = Field(
        description="'stored': the key is saved encrypted. 'browser': nothing secret is saved; "
        "the browser sends the key with each request."
    )
    model: str | None = Field(default=None, max_length=100)
    account_id: str | None = Field(default=None, max_length=64, description="Cloudflare only.")
    # SecretStr so it can never appear in a repr, a log line or a validation message.
    api_key: SecretStr | None = Field(default=None, max_length=400)


class ProviderOut(BaseModel):
    """What is saved. The key itself is never returned, only its last four characters."""

    slot: Literal["embedding", "chat"]
    kind: str
    mode: Literal["stored", "browser"]
    model: str | None
    account_id: str | None
    key_last4: str | None
    updated_at: datetime


class CatalogChat(BaseModel):
    id: str
    label: str
    default_model: str


class CatalogEmbedding(BaseModel):
    id: str
    label: str
    models: list[str]
    needs_account_id: bool


class ProvidersOut(BaseModel):
    chat_providers: list[CatalogChat]
    embedding_providers: list[CatalogEmbedding]
    embedding: ProviderOut | None
    chat: ProviderOut | None
