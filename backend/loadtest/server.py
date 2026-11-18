"""The real API for a load test, with exactly one change: the chat model is a fast fake.

    uvicorn loadtest.server:app --port 8200

A real model takes 10 to 30 seconds and would turn a load test into a test of the provider. The
fake answers instantly with one claim that quotes the first usable line of the first excerpt, so
retrieval, prompt building and the quote validator all run for real; only the model is absent.
Embeddings use whatever EMBEDDING_PROVIDER says (the load test uses the offline hash embedder).
"""

import json
import re

from app.api.deps import get_llm
from app.main import app

_HEADER = re.compile(r"^\[(C\d+)\] (.*)$", re.MULTILINE)


class FastLLM:
    last_model = "load-test-fake"

    def complete(self, system: str, user: str, **kw: object) -> str:
        block = user.split("<<<EXCERPTS", 1)[1]
        parts = _HEADER.split(block)
        for i in range(1, len(parts), 3):
            alias, code = parts[i], parts[i + 2]
            for line in code.split("\n"):
                text = line.strip()
                if len(text) >= 12 and "EXCERPTS>>>" not in text:
                    return json.dumps(
                        {
                            "cannot_determine": False,
                            "claims": [
                                {
                                    "text": f"It contains: {text[:60]}",
                                    "cites": [alias],
                                    "quote": text[:200],
                                }
                            ],
                            "missing": "",
                        }
                    )
        return json.dumps({"cannot_determine": True, "claims": [], "missing": "nothing usable"})


app.dependency_overrides[get_llm] = lambda: FastLLM()
