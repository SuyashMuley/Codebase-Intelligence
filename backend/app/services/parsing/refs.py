"""De-duplicating collector for call and import references."""

from typing import Any

from app.services.parsing.model import CALL, IMPORT, Ref
from app.services.parsing.util import MAX_CALLS_PER_FILE


class RefCollector:
    def __init__(self) -> None:
        self.refs: list[Ref] = []
        self._calls: dict[tuple[int | None, str | None, str], Ref] = {}
        self.truncated = False

    def add_import(self, name: str, line: int, symbol: int | None, detail: dict[str, Any]) -> None:
        if name:
            self.refs.append(Ref(IMPORT, name[:500], line, symbol, detail))

    def add_call(
        self,
        name: str,
        receiver: str | None,
        line: int,
        symbol: int | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record one call. Repeated calls of the same callee in the same symbol collapse into
        one ref with a count, which keeps the table small without losing what is called."""
        if not name:
            return
        key = (symbol, receiver, name)
        existing = self._calls.get(key)
        if existing is not None:
            existing.detail["count"] = existing.detail.get("count", 1) + 1
            return
        if len(self._calls) >= MAX_CALLS_PER_FILE:
            self.truncated = True
            return
        detail: dict[str, Any] = {"receiver": receiver, "count": 1}
        if extra:
            detail.update(extra)
        ref = Ref(CALL, name[:200], line, symbol, detail)
        self._calls[key] = ref
        self.refs.append(ref)
