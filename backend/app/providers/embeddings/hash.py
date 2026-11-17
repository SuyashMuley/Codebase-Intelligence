"""A deterministic, offline embedder (feature hashing of word tokens).

It is lexical, not semantic: texts that share identifiers and words end up close, synonyms do
not. It exists so the whole pipeline, the tests and the benchmark harness run with no account,
no network and no model download. Never present its results as semantic search.
"""

import hashlib
import math
import re
from collections import Counter

from app.providers.embeddings.base import l2_normalize

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokens(text: str) -> list[str]:
    """Lowercased words, with camelCase identifiers also split into their parts."""
    out: list[str] = []
    for word in _TOKEN.findall(text):
        out.append(word.lower())
        parts = _CAMEL.split(word)
        if len(parts) > 1:
            out.extend(p.lower() for p in parts)
    return out


class HashEmbedder:
    def __init__(self, dim: int = 384, batch_size: int = 64) -> None:
        self.dim = dim
        self.batch_size = batch_size
        self.model_id = f"hash-{dim}-v1"
        self.usage: dict[str, float] = {}

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        words = tokens(text)
        if not words:
            # Text with no words (just punctuation) would be the zero vector, whose cosine
            # distance is undefined. Hash the raw text instead so it still has a stable position.
            words = [f"#raw:{text}"]
        for token, count in Counter(words).items():
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            sign = 1.0 if (value >> 63) & 1 else -1.0  # a signed hash keeps collisions unbiased
            vector[value % self.dim] += sign * (1.0 + math.log(count))
        return l2_normalize(vector)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
