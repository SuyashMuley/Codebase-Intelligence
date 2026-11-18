"""Which embedding and chat provider a request may use for a repository.

A public repository uses the platform's providers. A private one uses only its owner's own: the
platform's keys are never used for private code, even when they are configured and the owner's
are missing (that is an error, not a fallback). Clients for a user's key are built only when
needed and closed when the request ends.
"""

import logging
from collections.abc import Callable, Mapping

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    AppError,
    EmbeddingNotConfigured,
    ProvidersRequired,
)
from app.db.models import Repository, User
from app.providers.embeddings.base import Embedder
from app.services.auth.crypto import TokenCipher
from app.services.providers import build, store
from app.services.qa.graph import ChatModel

logger = logging.getLogger(__name__)

EMBEDDING_KEY_HEADER = "x-embedding-key"
CHAT_KEY_HEADER = "x-chat-key"


class RepoProviders:
    def __init__(
        self,
        private: bool,
        make_embedder: Callable[[], Embedder | None],
        make_llm: Callable[[], ChatModel],
    ) -> None:
        self.private = private
        self._make_embedder = make_embedder
        self._make_llm = make_llm
        self._embedder: Embedder | None = None
        self._llm: ChatModel | None = None
        self._closers: list[Callable[[], None]] = []

    def embedder(self) -> Embedder | None:
        """The embedder, or None for a public repository when the platform has none (search then
        falls back to keywords). For a private repository, a missing provider is an error."""
        if self._embedder is None:
            self._embedder = self._make_embedder()
            self._track(self._embedder)
        return self._embedder

    def need_embedder(self) -> Embedder:
        embedder = self.embedder()
        if embedder is None:
            raise EmbeddingNotConfigured("Embeddings are not configured.")
        return embedder

    def need_llm(self) -> ChatModel:
        if self._llm is None:
            self._llm = self._make_llm()
            self._track(self._llm)
        return self._llm

    def need_both(self) -> None:
        """Both slots usable: required before a private repository is indexed."""
        self.need_embedder()
        self.need_llm()

    def _track(self, client: object) -> None:
        close = getattr(client, "close", None)
        if self.private and callable(close):  # platform clients are shared: never closed here
            self._closers.append(close)

    def close(self) -> None:
        for close in self._closers:
            try:
                close()
            except Exception:  # noqa: BLE001 - closing must never break a response
                logger.debug("client_close_failed")  # no detail: it could name a host or a key
        self._closers.clear()


def for_repository(
    repo: Repository,
    user: User,
    db: Session,
    headers: Mapping[str, str],
    settings: Settings,
    cipher: TokenCipher | None,
    platform_embedder: Embedder | None,
    platform_llm: ChatModel | AppError,
) -> RepoProviders:
    if not repo.is_private:

        def platform_llm_or_raise() -> ChatModel:
            if isinstance(platform_llm, AppError):
                raise platform_llm
            return platform_llm

        return RepoProviders(False, lambda: platform_embedder, platform_llm_or_raise)

    def user_embedder() -> Embedder:
        row = store.get(db, user.id, "embedding")
        if row is None:
            raise ProvidersRequired(
                "Private repositories need your own embedding provider. Add one in your "
                "provider settings."
            )
        key = store.read_key(row, cipher, headers.get(EMBEDDING_KEY_HEADER))
        return build.build_embedder(row, key, settings)

    def user_llm() -> ChatModel:
        row = store.get(db, user.id, "chat")
        if row is None:
            raise ProvidersRequired(
                "Private repositories need your own chat provider. Add one in your provider "
                "settings."
            )
        key = store.read_key(row, cipher, headers.get(CHAT_KEY_HEADER))
        return build.build_chat(row, key, settings)

    return RepoProviders(True, user_embedder, user_llm)
