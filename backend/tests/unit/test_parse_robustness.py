import random

import pytest

from app.services.parsing import parse as parse_module
from app.services.parsing.parse import parse_source
from tests.parse_helpers import check_chunks, check_symbols

LANGUAGES = [("a.py", "python"), ("a.js", "javascript"), ("a.ts", "typescript"), ("a.go", "go")]


@pytest.mark.parametrize(("path", "language"), LANGUAGES)
@pytest.mark.parametrize("content", ["", "\n", "   ", "\n\n\n", "﻿", "\x00", "}{)(", "###"])
def test_degenerate_input_never_raises(path: str, language: str, content: str) -> None:
    r = parse_source(path, language, content)
    assert r.line_count == len(content.split("\n"))


@pytest.mark.parametrize(("path", "language"), LANGUAGES)
def test_random_garbage_never_raises_and_chunks_still_cover_it(path: str, language: str) -> None:
    for seed in range(25):
        rng = random.Random(seed)
        alphabet = "abc (){}[]<>;:,.=+-*/\\\"'`#\n\n\t é日☃"
        content = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 600)))
        r = parse_source(path, language, content)
        check_symbols(r.parsed, r.line_count)
        check_chunks(content, r.chunks)


def test_deeply_nested_declarations_do_not_overflow_the_stack() -> None:
    depth = 300
    code = "".join("    " * i + f"class C{i}:\n" for i in range(depth)) + "    " * depth + "pass\n"
    r = parse_source("a.py", "python", code)
    check_symbols(r.parsed, r.line_count)
    assert len(r.parsed.symbols) <= 62  # nesting is capped, not followed forever


def test_deeply_nested_expressions_do_not_overflow_the_stack() -> None:
    code = "x = " + "(" * 3000 + "1" + ")" * 3000 + "\n"
    r = parse_source("a.py", "python", code)
    assert r.line_count == 2
    js = "const x = " + "[" * 3000 + "1" + "]" * 3000 + ";\n"
    assert parse_source("a.js", "javascript", js).line_count == 2


def test_a_big_file_parses_quickly() -> None:
    import time

    body = "\n".join(
        f"def f{i}(a, b):\n    return helper(a) + other.call(b, {i})\n" for i in range(4000)
    )
    started = time.monotonic()
    r = parse_source("big.py", "python", body)
    assert time.monotonic() - started < 5
    assert len(r.parsed.symbols) == 4000
    check_chunks(body, r.chunks)


def test_a_parse_that_runs_out_of_time_is_marked_truncated_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parse_module, "PARSE_TIMEOUT_SECONDS", -1.0)
    code = "def f():\n    return 1\n" * 5
    r = parse_source("a.py", "python", code)
    assert r.parsed.truncated is True
    check_chunks(code, r.chunks)  # still chunked, so the file stays searchable


def test_the_call_table_is_capped_per_file() -> None:
    code = "def f():\n" + "".join(f"    fn{i}()\n" for i in range(6000))
    r = parse_source("a.py", "python", code)
    assert len([x for x in r.parsed.refs if x.kind == "call"]) == 5000
    assert r.parsed.truncated is True


def test_crlf_files_keep_working_line_numbers() -> None:
    # Fetching normalises CRLF, but a stray carriage return must never shift line numbers.
    code = "def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n"
    r = parse_source("a.py", "python", code)
    assert [(s.name, s.start_line, s.end_line) for s in r.parsed.symbols] == [
        ("a", 1, 2),
        ("b", 4, 5),
    ]
