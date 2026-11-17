"""Encryption helpers. Keys come from settings and are never logged."""

import base64
import hashlib
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken


class CipherError(Exception):
    """A value could not be decrypted (wrong key, tampered, or expired)."""


class TokenCipher:
    """Encrypts GitHub tokens at rest (Fernet: AES-128-CBC with HMAC-SHA256)."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise CipherError("could not decrypt value") from None


class StateSealer:
    """Seals the short-lived OAuth login state (state, PKCE verifier, next URL) into a cookie.

    Encrypted and authenticated, so the browser can neither read the PKCE verifier nor forge
    or alter the value. Fernet embeds a timestamp that is checked against a TTL on open.
    """

    def __init__(self, secret: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._fernet = Fernet(key)

    def seal(self, value: str, now: float | None = None) -> str:
        data = value.encode()
        if now is None:
            return self._fernet.encrypt(data).decode()
        return self._fernet.encrypt_at_time(data, int(now)).decode()

    def open(self, sealed: str, ttl_seconds: int, now: float | None = None) -> str:
        try:
            current = int(time.time() if now is None else now)
            return self._fernet.decrypt_at_time(sealed.encode(), ttl_seconds, current).decode()
        except InvalidToken:
            raise CipherError("invalid or expired state") from None


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_pkce_verifier() -> str:
    return secrets.token_urlsafe(48)  # 64 chars, within RFC 7636's 43 to 128


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
