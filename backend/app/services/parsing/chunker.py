"""Syntax-aware chunking.

A chunk is a contiguous range of whole lines of one file, so a citation (path, start line, end
line) always points at exactly the text that was embedded and shown to the model.

Guarantees (all tested):
- Every non-blank line of the file is in exactly one chunk (no gaps, no overlaps).
- Chunk text is the exact source lines, nothing added.
- Chunks start and end on non-blank lines.
- A chunk is at most `max_chars`, unless a single line is longer than that.

Strategy: one chunk per top-level symbol; a symbol too big for one chunk is split into its
members (a class becomes a header chunk plus one chunk per method); a symbol with no members
that is still too big is cut into windows at blank lines. Code between symbols (imports,
constants, scripts) becomes its own chunks, and neighbouring tiny chunks are merged so a file
full of one-line functions does not become hundreds of useless chunks.
"""

from dataclasses import dataclass

from app.services.parsing.model import Symbol


@dataclass(frozen=True)
class Chunk:
    start_line: int  # 1-based, inclusive
    end_line: int
    symbol: int | None  # index of the primary symbol in the file's symbol list


@dataclass
class _Piece:
    start: int
    end: int
    symbol: int | None


def build_chunks(
    lines: list[str],
    symbols: list[Symbol],
    max_chars: int = 1800,
    merge_below: int = 500,
) -> list[Chunk]:
    n = len(lines)
    if n == 0:
        return []

    # prefix[i] = characters in lines[0:i] counting a newline per line
    prefix = [0]
    for line in lines:
        prefix.append(prefix[-1] + len(line) + 1)

    def size(a: int, b: int) -> int:  # characters in lines a..b (1-based, inclusive)
        return prefix[b] - prefix[a - 1] - 1

    def blank(i: int) -> bool:  # 1-based
        return not lines[i - 1].strip()

    def trim(a: int, b: int) -> tuple[int, int] | None:
        while a <= b and blank(a):
            a += 1
        while b >= a and blank(b):
            b -= 1
        return (a, b) if a <= b else None

    def windows(a: int, b: int) -> list[tuple[int, int]]:
        """Cut a..b into pieces of at most max_chars, preferring to cut at a blank line."""
        out: list[tuple[int, int]] = []
        cur = a
        while cur <= b:
            if size(cur, b) <= max_chars:
                out.append((cur, b))
                break
            end = cur
            while end < b and size(cur, end + 1) <= max_chars:
                end += 1
            # `end` is the last line that still fits (at least `cur`, even if it alone is huge)
            cut = end
            midpoint = cur + (end - cur) // 2
            for i in range(end, midpoint, -1):
                if blank(i):
                    cut = i - 1  # end the piece just before the blank line
                    break
            if cut < cur:
                cut = cur
            out.append((cur, cut))
            cur = cut + 1
        return out

    pieces: list[_Piece] = []

    def emit(a: int, b: int, symbol: int | None) -> None:
        trimmed = trim(a, b)
        if trimmed is None:
            return
        for wa, wb in windows(*trimmed):
            t = trim(wa, wb)
            if t is not None:
                pieces.append(_Piece(t[0], t[1], symbol))

    children: dict[int | None, list[int]] = {}
    for idx, sym in enumerate(symbols):
        children.setdefault(sym.parent, []).append(idx)
    for kids in children.values():
        kids.sort(key=lambda i: (symbols[i].start_line, symbols[i].end_line))

    def level(owner: int | None, lo: int, hi: int, depth: int = 0) -> None:
        cursor = lo
        for idx in children.get(owner, []):
            sym = symbols[idx]
            start, end = max(sym.start_line, cursor), min(sym.end_line, hi)
            if end < start:
                continue  # overlaps something already emitted: skip rather than duplicate lines
            if cursor < start:
                emit(cursor, start - 1, owner)
            members = children.get(idx)
            if members and size(start, end) > max_chars and depth < 20:
                level(idx, start, end, depth + 1)
            else:
                emit(start, end, idx)
            cursor = end + 1
        if cursor <= hi:
            emit(cursor, hi, owner)

    level(None, 1, n)

    pieces.sort(key=lambda p: p.start)
    return _merge(pieces, size, max_chars, merge_below)


def _merge(pieces: list[_Piece], size, max_chars: int, merge_below: int) -> list[Chunk]:  # type: ignore[no-untyped-def]
    """Join neighbouring small pieces so tiny functions share a chunk. The first piece's symbol
    (or, failing that, the next one's) stays as the chunk's primary symbol."""
    merged: list[_Piece] = []
    for piece in pieces:
        if merged:
            last = merged[-1]
            small = (
                size(last.start, last.end) < merge_below
                and size(piece.start, piece.end) < merge_below
            )
            if small and size(last.start, piece.end) <= max_chars:
                last.end = piece.end
                if last.symbol is None:
                    last.symbol = piece.symbol
                continue
        merged.append(_Piece(piece.start, piece.end, piece.symbol))
    return [Chunk(p.start, p.end, p.symbol) for p in merged]
