import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.api.auth import DEFAULT_NEXT, safe_next
from app.core.config import Settings
from app.services.auth.crypto import (
    CipherError,
    StateSealer,
    TokenCipher,
    new_pkce_verifier,
    new_state,
    pkce_challenge,
)


def test_token_cipher_round_trip_and_ciphertext_differs() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode())
    secret = "gho_16C7e42F292c6912E7710c838347Ae178B4a"
    sealed = cipher.encrypt(secret)
    assert secret not in sealed
    assert cipher.decrypt(sealed) == secret
    assert cipher.encrypt(secret) != sealed  # random IV: no equality leak


def test_token_cipher_rejects_tampering_and_wrong_key() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode())
    sealed = cipher.encrypt("secret")
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(CipherError):
        cipher.decrypt(tampered)
    with pytest.raises(CipherError):
        TokenCipher(Fernet.generate_key().decode()).decrypt(sealed)
    with pytest.raises(CipherError):
        cipher.decrypt("not-a-token")


def test_state_sealer_round_trip_and_hides_contents() -> None:
    sealer = StateSealer("secret-one")
    sealed = sealer.seal('{"s": "state-value", "v": "pkce-verifier"}')
    assert "state-value" not in sealed and "pkce-verifier" not in sealed
    assert sealer.open(sealed, ttl_seconds=600) == '{"s": "state-value", "v": "pkce-verifier"}'


def test_state_sealer_expiry_and_forgery() -> None:
    sealer = StateSealer("secret-one")
    sealed = sealer.seal("payload", now=1_000)
    assert sealer.open(sealed, ttl_seconds=600, now=1_500) == "payload"
    with pytest.raises(CipherError):
        sealer.open(sealed, ttl_seconds=600, now=1_700)  # older than the TTL
    with pytest.raises(CipherError):
        StateSealer("another-secret").open(sealed, ttl_seconds=600, now=1_500)
    with pytest.raises(CipherError):
        sealer.open(sealed[:-3] + "xyz", ttl_seconds=600, now=1_500)


def test_pkce_challenge_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_values_are_unique_and_long_enough() -> None:
    assert len({new_state() for _ in range(50)}) == 50
    verifier = new_pkce_verifier()
    assert 43 <= len(verifier) <= 128
    assert len(new_state()) >= 43  # 256 bits


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/repos", "/repos"),
        ("/repos/abc?tab=1", "/repos/abc?tab=1"),
        (None, DEFAULT_NEXT),
        ("", DEFAULT_NEXT),
        ("repos", DEFAULT_NEXT),
        ("//evil.example", DEFAULT_NEXT),
        ("https://evil.example", DEFAULT_NEXT),
        ("javascript:alert(1)", DEFAULT_NEXT),
        ("/\\evil.example", DEFAULT_NEXT),
        ("/ok\nSet-Cookie: x=1", DEFAULT_NEXT),
    ],
)
def test_safe_next_blocks_open_redirects(value: str | None, expected: str) -> None:
    assert safe_next(value) == expected


def test_settings_reject_a_bad_encryption_key_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(token_encryption_key="not-a-fernet-key-but-secret-looking")
    assert "secret-looking" not in str(exc.value)


def test_settings_blank_values_are_unset() -> None:
    s = Settings(github_app_client_id="", session_secret="  ", github_token="")
    assert s.github_app_client_id is None
    assert s.session_secret is None and s.github_token is None
    assert not s.auth_configured


def test_production_requires_secure_configuration() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            environment="production",
            github_app_client_id=None,
            github_app_client_secret=None,
            session_secret=None,
            token_encryption_key=None,
        )
    message = str(exc.value)
    for needle in ("GitHub App credentials", "COOKIE_SECURE", "https", "CLIENT_KEY_SALT"):
        assert needle in message

    ok = Settings(
        environment="production",
        github_app_client_id="id",
        github_app_client_secret="secret",
        session_secret="s" * 32,
        token_encryption_key=Fernet.generate_key().decode(),
        cookie_secure=True,
        app_base_url="https://app.example.com",
        client_key_salt="a-real-salt",
    )
    assert ok.auth_configured
    assert ok.callback_url == "https://app.example.com/api/auth/github/callback"


def test_allowed_origins_normalize_trailing_slashes() -> None:
    s = Settings(app_base_url="https://app.example.com/", cors_origins="http://localhost:3000/")
    assert s.allowed_origins == {"https://app.example.com", "http://localhost:3000"}


def test_env_files_are_found_regardless_of_working_directory(
    tmp_path: pytest.TempPathFactory,
) -> None:
    from app.core.config import ENV_FILES

    names = [(p.parent.name, p.name) for p in ENV_FILES]
    assert names[0][1] == ".env" and names[1] == ("backend", ".env")
    assert all(p.is_absolute() for p in ENV_FILES)  # not relative to wherever uvicorn was started


def test_backend_env_overrides_the_root_env(tmp_path: pytest.TempPathFactory) -> None:
    from pathlib import Path

    root = Path(str(tmp_path)) / "root.env"
    backend = Path(str(tmp_path)) / "backend.env"
    root.write_text("GITHUB_APP_CLIENT_ID=from-root\nAPP_BASE_URL=http://root.example\n")
    backend.write_text("APP_BASE_URL=http://backend.example\n")
    # Process env vars (set by the test config) would win over both files, so look at the files
    # in isolation by clearing those two names for this construction.
    import os

    saved = {k: os.environ.pop(k) for k in ("GITHUB_APP_CLIENT_ID", "APP_BASE_URL")}
    try:
        s = Settings(_env_file=(root, backend))
    finally:
        os.environ.update(saved)
    assert s.github_app_client_id == "from-root"  # only in the root file: still found
    assert s.app_base_url == "http://backend.example"  # in both: backend/.env wins
