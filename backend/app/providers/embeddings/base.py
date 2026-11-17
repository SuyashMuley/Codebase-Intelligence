"""The embedding interface the rest of the app depends on. Providers are interchangeable."""

import math
from typing import Protocol


class Embedder(Protocol):
    #: Stored with every vector, so vectors from different models are never mixed.
    model_id: str
    dim: int
    #: Texts sent per request.
    batch_size: int
    #: Running totals for cost visibility, e.g. {"requests": 3, "neurons": 1.4}.
    usage: dict[str, float]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed texts to be searched (code chunks). One vector per text, in order."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query. Some models want a different treatment than documents."""
        ...


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else vector
