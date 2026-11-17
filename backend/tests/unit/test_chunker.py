import random

import pytest

from app.services.parsing.chunker import build_chunks
from app.services.parsing.model import Symbol
from app.services.parsing.parse import parse_source
from tests.parse_helpers import check_chunks, parse_ok


def sym(start: int, end: int, parent: int | None = None, name: str = "s") -> Symbol:
    return Symbol("function", name, f"f.py::{name}", start, end, parent, "def s():")


def lines_of(*parts: str) -> list[str]:
    return "\n".join(parts).split("\n")


def test_a_small_file_is_one_chunk() -> None:
    code = "import os\n\n\ndef f():\n    return 1\n"
    r = parse_ok("a.py", "python", code)
    assert [(c.start_line, c.end_line) for c in r.chunks] == [(1, 5)]


def test_empty_and_blank_files_have_no_chunks() -> None:
    assert build_chunks([], []) == []
    assert build_chunks(["", "  ", "\t"], []) == []


def test_chunks_start_and_end_on_code_not_blank_lines() -> None:
    lines = ["", "", "x = 1", "", "y = 2", "", ""]
    chunks = build_chunks(lines, [])
    assert [(c.start_line, c.end_line) for c in chunks] == [(3, 5)]


def test_each_symbol_gets_its_own_chunk_when_they_are_big_enough() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(30))  # ~330 chars
    code = f"def a():\n{body}\n\n\ndef b():\n{body}\n"
    r = parse_ok("a.py", "python", code, max_chars=500, merge_below=100)
    a, b = r.parsed.symbols
    assert [(c.start_line, c.end_line, c.symbol) for c in r.chunks] == [
        (a.start_line, a.end_line, 0),
        (b.start_line, b.end_line, 1),
    ]


def test_a_class_too_big_for_one_chunk_splits_into_header_and_methods() -> None:
    method = "\n".join(f"        step{i}()" for i in range(20))
    code = (
        '"""doc"""\n'
        "class Big:\n"
        '    """class doc"""\n'
        "    attr = 1\n\n"
        f"    def one(self):\n{method}\n\n"
        f"    def two(self):\n{method}\n\n"
        "    tail = 2\n"
    )
    r = parse_ok("a.py", "python", code, max_chars=400, merge_below=50)
    syms = {s.name: s for s in r.parsed.symbols}
    chunks = [(c.start_line, c.end_line, c.symbol) for c in r.chunks]
    one, two = syms["one"], syms["two"]
    assert (one.start_line, one.end_line, 1) in chunks  # a method is one whole chunk
    assert (two.start_line, two.end_line, 2) in chunks
    header = next(c for c in chunks if c[0] <= 3 <= c[1])
    assert header[1] < one.start_line  # the class header stops before the first method
    assert any(c[0] > two.end_line for c in chunks)  # and the trailing attribute is covered
    # Header and tail chunks belong to the class itself.
    assert header[2] == 0 and chunks[-1][2] == 0


def test_a_huge_function_is_cut_into_windows_at_blank_lines() -> None:
    blocks = ["\n".join(f"    value_{b}_{i} = {i}" for i in range(8)) for b in range(12)]
    code = "def huge():\n" + "\n\n".join(blocks) + "\n"
    r = parse_ok("a.py", "python", code, max_chars=500, merge_below=0)
    assert len(r.chunks) > 3
    lines = code.split("\n")
    for c in r.chunks:
        # Every window ends right before a blank line, never mid-block.
        if c.end_line < len(lines) - 1:
            assert not lines[c.end_line].strip(), (c, lines[c.end_line])
        assert c.symbol == 0


def test_a_single_overlong_line_is_kept_whole() -> None:
    code = "DATA = [" + ",".join(str(i) for i in range(2000)) + "]\n"
    r = parse_ok("a.py", "python", code, max_chars=500)
    assert [(c.start_line, c.end_line) for c in r.chunks] == [(1, 1)]


def test_code_between_symbols_is_its_own_chunk() -> None:
    func = "\n".join(f"    line{i} = {i}" for i in range(25))
    code = f"def a():\n{func}\n\nSETTING = 'between'\nOTHER = 2\n\n\ndef b():\n{func}\n"
    r = parse_ok("a.py", "python", code, max_chars=450, merge_below=60)
    texts = ["\n".join(code.split("\n")[c.start_line - 1 : c.end_line]) for c in r.chunks]
    middle = [t for t in texts if "SETTING" in t]
    assert len(middle) == 1 and "def " not in middle[0]


def test_tiny_neighbours_are_merged_so_one_line_functions_do_not_explode_the_count() -> None:
    code = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(40))
    r = parse_ok("a.py", "python", code)
    assert len(r.chunks) < 10  # 40 symbols, but far fewer chunks
    assert len(r.parsed.symbols) == 40


def test_merged_chunks_keep_the_first_symbol_as_primary() -> None:
    code = "import os\n\ndef a():\n    pass\n\ndef b():\n    pass\n"
    r = parse_ok("a.py", "python", code)
    assert len(r.chunks) == 1
    assert r.chunks[0].symbol == 0  # `a`, the first symbol (the import gap has none)


def test_chunking_is_deterministic() -> None:
    code = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(50))
    first = parse_source("a.py", "python", code).chunks
    assert all(parse_source("a.py", "python", code).chunks == first for _ in range(3))


def test_overlapping_or_out_of_order_symbols_never_duplicate_lines() -> None:
    lines = [f"line {i}" for i in range(1, 21)]
    weird = [sym(5, 15, name="outer"), sym(8, 12, name="dup"), sym(10, 18, name="overlap")]
    chunks = build_chunks(lines, weird, max_chars=60, merge_below=0)
    check_chunks("\n".join(lines), chunks, max_chars=60)


@pytest.mark.parametrize("seed", range(300))
def test_random_files_always_satisfy_the_chunking_guarantees(seed: int) -> None:
    """Property test: for arbitrary text and arbitrary (even nested) symbols, every non-blank
    line is covered exactly once, chunks start and end on code, and sizes are respected."""
    rng = random.Random(seed)
    n = rng.randint(1, 120)
    lines = [
        ""
        if rng.random() < 0.2
        else "x" * rng.randint(1, 90) + ("  " if rng.random() < 0.1 else "")
        for _ in range(n)
    ]
    symbols: list[Symbol] = []
    for _ in range(rng.randint(0, 8)):
        a = rng.randint(1, n)
        b = rng.randint(a, min(n, a + rng.randint(0, 40)))
        parent = None
        if symbols and rng.random() < 0.5:
            cand = rng.randrange(len(symbols))
            p = symbols[cand]
            a, b = max(a, p.start_line), min(b, p.end_line)
            if a <= b:
                parent = cand
        if a <= b:
            symbols.append(sym(a, b, parent, name=f"s{len(symbols)}"))
    max_chars = rng.choice([60, 150, 400, 1800])
    merge_below = rng.choice([0, 40, 200])
    chunks = build_chunks(lines, symbols, max_chars, merge_below)
    check_chunks("\n".join(lines), chunks, max_chars)
