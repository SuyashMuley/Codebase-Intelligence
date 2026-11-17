"""Resolve a question's relevance labels against the symbols the pipeline actually extracted."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class LabelError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    path: str
    ranges: tuple[tuple[int, int], ...] | None  # None: any chunk of the file counts


def resolve(db: Session, snapshot_id: uuid.UUID, labels: list[dict[str, Any]]) -> list[Target]:
    """A label is {"path"} (the whole file) or {"path", "symbol"} (every symbol with that
    qualified name; overloads and their implementation all count). A label that matches nothing
    is an error, so a stale dataset cannot silently score zero."""
    targets: list[Target] = []
    for label in labels:
        path = label["path"]
        if "symbol" not in label:
            exists = db.scalar(
                text("SELECT 1 FROM files WHERE snapshot_id = :s AND path = :p"),
                {"s": snapshot_id, "p": path},
            )
            if not exists:
                raise LabelError(f"file not found: {path}")
            targets.append(Target(path, None))
            continue
        rows = db.execute(
            text(
                "SELECT s.start_line, s.end_line FROM symbols s JOIN files f ON f.id = s.file_id "
                "WHERE s.snapshot_id = :s AND f.path = :p AND s.qualified_name = :q"
            ),
            {"s": snapshot_id, "p": path, "q": f"{path}::{label['symbol']}"},
        ).all()
        if not rows:
            raise LabelError(f"symbol not found: {path}::{label['symbol']}")
        targets.append(Target(path, tuple((int(a), int(b)) for a, b in rows)))
    return targets


def is_relevant(path: str, start: int, end: int, targets: list[Target]) -> bool:
    """A retrieved chunk is relevant if it overlaps a labelled symbol (or is in a labelled file)."""
    for t in targets:
        if t.path != path:
            continue
        if t.ranges is None:
            return True
        if any(start <= hi and lo <= end for lo, hi in t.ranges):
            return True
    return False
