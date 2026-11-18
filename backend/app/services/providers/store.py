"""Saving, validating and reading a user's provider settings.

A key is accepted once, encrypted with the server's TOKEN_ENCRYPTION_KEY (Fernet) and never
returned: the API shows only the provider, the model and the last four characters. Browser-held
mode saves no key at all.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.errors import InvalidProviderConfig, ProvidersRequired
from app.db.models import UserProvider
from app.services.auth.crypto import CipherError, TokenCipher
from app.services.providers.catalog import (
    ACCOUNT_ID_RE,
    CHAT_PROVIDERS,
    EMBEDDING_PROVIDERS,
    MODEL_RE,
    MODES,
    SLOTS,
)

# Keys travel in HTTP headers in browser mode, so they must be plain printable ASCII.
_KEY_RE = re.compile(r"^[\x21-\x7e]{8,400}$")

SLOT_LABEL = {"embedding": "embedding", "chat": "chat"}


@dataclass(frozen=True)
class Settings:
    kind: str
    mode: str
    model: str | None
    account_id: str | None


def validate(
    slot: str,
    kind: str,
    mode: str,
    model: str | None,
    account_id: str | None,
    api_key: str | None,
) -> Settings:
    """Check a submitted configuration and fill in defaults. Raises InvalidProviderConfig."""
    if slot not in SLOTS:
        raise InvalidProviderConfig("Unknown provider slot.")
    if mode not in MODES:
        raise InvalidProviderConfig("Mode must be 'stored' or 'browser'.")
    if slot == "chat":
        spec = CHAT_PROVIDERS.get(kind)
        if spec is None:
            raise InvalidProviderConfig(
                f"Unsupported chat provider. Choose one of: {_ids(CHAT_PROVIDERS)}."
            )
        model = (model or spec.default_model).strip()
        if not MODEL_RE.match(model):
            raise InvalidProviderConfig("That model name is not valid.")
        account_id = None
    else:
        espec = EMBEDDING_PROVIDERS.get(kind)
        if espec is None:
            raise InvalidProviderConfig(
                f"Unsupported embedding provider. Choose one of: {_ids(EMBEDDING_PROVIDERS)}."
            )
        model = (model or espec.models[0]).strip()
        if model not in espec.models:
            raise InvalidProviderConfig(
                f"Only these models give the 384-dimension vectors the index needs: "
                f"{', '.join(espec.models)}."
            )
        if espec.needs_account_id:
            account_id = (account_id or "").strip().lower()
            if not ACCOUNT_ID_RE.match(account_id):
                raise InvalidProviderConfig(
                    "The Cloudflare account id is 32 lowercase hex characters."
                )
        else:
            account_id = None
    if mode == "stored":
        if api_key is None or not _KEY_RE.match(api_key):
            raise InvalidProviderConfig(
                "Enter the API key (8 to 400 printable characters, no spaces)."
            )
    elif api_key is not None:
        raise InvalidProviderConfig(
            "In browser-held mode the key is not sent to the server when saving."
        )
    return Settings(kind=kind, mode=mode, model=model, account_id=account_id)


def _ids(table: Mapping[str, object]) -> str:
    return ", ".join(table)


def save(
    db: Session,
    user_id: uuid.UUID,
    slot: str,
    cfg: Settings,
    api_key: str | None,
    cipher: TokenCipher,
) -> UserProvider:
    row = db.get(UserProvider, (user_id, slot))
    if row is None:
        row = UserProvider(user_id=user_id, slot=slot, kind=cfg.kind, mode=cfg.mode)
        db.add(row)
    row.kind, row.mode, row.model, row.account_id = cfg.kind, cfg.mode, cfg.model, cfg.account_id
    if cfg.mode == "stored":
        assert api_key is not None
        row.key_enc = cipher.encrypt(api_key)
        row.key_last4 = api_key[-4:]
    else:
        row.key_enc = None
        row.key_last4 = None
    db.commit()
    return row


def get(db: Session, user_id: uuid.UUID, slot: str) -> UserProvider | None:
    return db.get(UserProvider, (user_id, slot))


def remove(db: Session, user_id: uuid.UUID, slot: str) -> None:
    db.execute(
        delete(UserProvider).where(UserProvider.user_id == user_id, UserProvider.slot == slot)
    )
    db.commit()


def read_key(row: UserProvider, cipher: TokenCipher | None, header_value: str | None) -> str:
    """The key to use for this request: decrypted, or the one the browser sent. Never logged."""
    slot = SLOT_LABEL[row.slot]
    if row.mode == "browser":
        if not header_value or not _KEY_RE.match(header_value):
            raise ProvidersRequired(
                f"Your {slot} key is kept in this browser only and was not sent with this request. "
                "Enter it again in your provider settings."
            )
        return header_value
    if cipher is None or row.key_enc is None:
        raise ProvidersRequired(f"Your {slot} provider key is not available. Save it again.")
    try:
        return cipher.decrypt(row.key_enc)
    except CipherError:
        # The server's encryption key changed: the stored key is unreadable.
        raise ProvidersRequired(
            f"Your stored {slot} key can no longer be read. Save it again in provider settings."
        ) from None
