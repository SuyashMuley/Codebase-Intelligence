"""Your own embedding and chat providers, used for your private repositories."""

from typing import Annotated, Any

from fastapi import Depends, Response, status

from app.api.deps import (
    CurrentUser,
    DbSession,
    csrf_guard,
    get_cipher,
    ip_backstop,
    user_general_limit,
)
from app.core.dbrelease import ReleasingRouter
from app.core.errors import InvalidProviderConfig
from app.db.models import UserProvider
from app.schemas.providers import (
    CatalogChat,
    CatalogEmbedding,
    ProviderIn,
    ProviderOut,
    ProvidersOut,
)
from app.schemas.repository import ErrorEnvelope
from app.services import audit
from app.services.auth.crypto import TokenCipher
from app.services.providers import store
from app.services.providers.catalog import CHAT_PROVIDERS, EMBEDDING_PROVIDERS, SLOTS

router = ReleasingRouter(
    prefix="/api/providers",
    tags=["providers"],
    dependencies=[Depends(ip_backstop), Depends(csrf_guard), Depends(user_general_limit)],
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in"},
    422: {"model": ErrorEnvelope, "description": "Invalid provider settings"},
}


def _out(row: UserProvider) -> ProviderOut:
    return ProviderOut(
        slot=row.slot,
        kind=row.kind,
        mode=row.mode,
        model=row.model,
        account_id=row.account_id,
        key_last4=row.key_last4,
        updated_at=row.updated_at,
    )


def _slot(slot: str) -> str:
    if slot not in SLOTS:
        raise InvalidProviderConfig("Unknown provider slot. Use 'embedding' or 'chat'.")
    return slot


@router.get("", response_model=ProvidersOut, responses=_ERRORS)
def get_providers(db: DbSession, user: CurrentUser) -> ProvidersOut:
    """The providers you can choose from, and what you have saved. Keys are never returned."""
    emb = store.get(db, user.id, "embedding")
    chat = store.get(db, user.id, "chat")
    return ProvidersOut(
        chat_providers=[
            CatalogChat(id=p.id, label=p.label, default_model=p.default_model)
            for p in CHAT_PROVIDERS.values()
        ],
        embedding_providers=[
            CatalogEmbedding(
                id=p.id,
                label=p.label,
                models=list(p.models),
                needs_account_id=p.needs_account_id,
            )
            for p in EMBEDDING_PROVIDERS.values()
        ],
        embedding=_out(emb) if emb else None,
        chat=_out(chat) if chat else None,
    )


@router.put("/{slot}", response_model=ProviderOut, responses=_ERRORS)
def put_provider(
    slot: str,
    body: ProviderIn,
    db: DbSession,
    user: CurrentUser,
    cipher: Annotated[TokenCipher, Depends(get_cipher)],
) -> ProviderOut:
    """Save the provider for `embedding` or `chat`. In 'stored' mode the key is encrypted at rest
    and never shown again; in 'browser' mode no key is sent here at all."""
    key = body.api_key.get_secret_value() if body.api_key is not None else None
    cfg = store.validate(_slot(slot), body.kind, body.mode, body.model, body.account_id, key)
    row = store.save(db, user.id, slot, cfg, key, cipher)
    audit.record(
        db, "provider.save", user.id, None, {"slot": slot, "kind": cfg.kind, "mode": cfg.mode}
    )
    return _out(row)


@router.delete("/{slot}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
def delete_provider(slot: str, db: DbSession, user: CurrentUser) -> Response:
    """Remove the saved provider (and its stored key). Private repositories that need it stop
    working until you add another."""
    store.remove(db, user.id, _slot(slot))
    audit.record(db, "provider.remove", user.id, None, {"slot": slot})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
