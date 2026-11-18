import types
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet

from app.core.errors import InvalidProviderConfig, ProvidersRequired
from app.db.models import UserProvider
from app.services.auth.crypto import TokenCipher
from app.services.providers import store

KEY = "sk-test-key-0123456789"
ACCOUNT = "0123456789abcdef0123456789abcdef"


def v(**over: object) -> store.Settings:
    args: dict[str, Any] = {
        "slot": "chat",
        "kind": "openai",
        "mode": "stored",
        "model": None,
        "account_id": None,
        "api_key": KEY,
    }
    args.update(over)
    return store.validate(**args)


def test_defaults_are_filled_in() -> None:
    assert v().model == "gpt-4o-mini" and v().account_id is None
    emb = v(slot="embedding", kind="openai")
    assert emb.model == "text-embedding-3-small"
    cf = v(slot="embedding", kind="cloudflare", account_id=ACCOUNT.upper())
    assert cf.model == "@cf/baai/bge-small-en-v1.5" and cf.account_id == ACCOUNT  # normalized


@pytest.mark.parametrize(
    "over",
    [
        {"slot": "images"},
        {"mode": "somewhere"},
        {"kind": "anthropic"},  # not in the catalogue
        {"kind": "https://evil.example"},  # a URL is not a provider
        {"kind": "openai", "slot": "embedding", "model": "text-embedding-ada-002"},  # not 384-able
        {"slot": "embedding", "kind": "cloudflare", "account_id": "short"},
        {"slot": "embedding", "kind": "cloudflare", "account_id": None},
        {"slot": "embedding", "kind": "cloudflare", "account_id": ACCOUNT, "model": "other"},
        {"model": "bad model name"},
        {"model": "../../etc/passwd"},
        {"model": "x" * 101},
        {"api_key": None},
        {"api_key": "short"},
        {"api_key": "has a space in it 12345"},
        {"api_key": "line\nbreak-0123456789"},
        {"api_key": "tab\tkey-0123456789"},
        {"api_key": "é" * 20},
        {"mode": "browser"},  # a browser-held key must not be sent for saving
    ],
)
def test_invalid_settings_are_refused(over: dict[str, Any]) -> None:
    with pytest.raises(InvalidProviderConfig):
        v(**over)


def test_browser_mode_saves_no_key() -> None:
    cfg = v(mode="browser", api_key=None)
    assert cfg.mode == "browser"


def row(mode: str, enc: str | None = None, slot: str = "chat") -> UserProvider:
    return cast(UserProvider, types.SimpleNamespace(slot=slot, mode=mode, key_enc=enc))


def test_stored_keys_are_decrypted_with_the_servers_key() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode())
    assert store.read_key(row("stored", cipher.encrypt(KEY)), cipher, None) == KEY
    assert (
        store.read_key(row("stored", cipher.encrypt(KEY)), cipher, "ignored-header-0123456789")
        == KEY
    )


def test_a_stored_key_that_cannot_be_read_asks_for_it_again() -> None:
    other = TokenCipher(Fernet.generate_key().decode())
    cipher = TokenCipher(Fernet.generate_key().decode())
    with pytest.raises(ProvidersRequired) as exc:
        store.read_key(row("stored", other.encrypt(KEY)), cipher, None)
    assert "Save it again" in exc.value.message and KEY not in exc.value.message
    with pytest.raises(ProvidersRequired):
        store.read_key(row("stored", None), cipher, None)
    with pytest.raises(ProvidersRequired):
        store.read_key(row("stored", other.encrypt(KEY)), None, None)


def test_a_browser_held_key_must_arrive_with_the_request() -> None:
    assert store.read_key(row("browser"), None, KEY) == KEY
    for header in (None, "", "short", "has space in it 123456", "x" * 401):
        with pytest.raises(ProvidersRequired) as exc:
            store.read_key(row("browser"), None, header)
        assert "this browser" in exc.value.message
