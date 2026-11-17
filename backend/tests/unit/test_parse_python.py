from app.services.parsing.parse import parse_source
from tests.parse_helpers import calls, imports, parse_ok, qualified, sym_by_name, symbols

SAMPLE = '''"""Module docstring."""
import os, sys as system
from .pkg import (a, b as c)
from . import x
from ..up.deep import *
from collections import OrderedDict

CONST = 1


# Helper docs
# on two lines
def helper(x):
    return os.path.join(x)


@decorator
@other(1)
class Foo(Base):
    """doc"""

    attr = 1

    def method(self, a):
        self.helper(a)
        helper(a)
        helper(a)

    @staticmethod
    async def amethod():
        def inner():
            deep()
        inner()

    class Nested:
        def m(self): ...


async def top():
    Foo().method(1)


if TYPE_CHECKING:
    def conditional(): ...
else:
    def fallback(): ...

try:
    import fast
except ImportError:
    def slow(): ...
'''


def test_symbols_with_exact_ranges() -> None:
    r = parse_ok("pkg/mod.py", "python", SAMPLE)
    assert symbols(r.parsed) == [
        ("function", "helper", 11, 14),  # includes the two comment lines above
        ("class", "Foo", 17, 36),  # includes both decorators
        ("method", "method", 24, 27),
        ("method", "amethod", 29, 33),  # includes @staticmethod
        ("class", "Nested", 35, 36),
        ("method", "m", 36, 36),
        ("function", "top", 39, 40),
        ("function", "conditional", 44, 44),
        ("function", "fallback", 46, 46),
        ("function", "slow", 51, 51),
    ]
    assert qualified(r.parsed) == [
        "helper", "Foo", "Foo.method", "Foo.amethod", "Foo.Nested", "Foo.Nested.m", "top",
        "conditional", "fallback", "slow",
    ]  # fmt: skip


def test_nested_functions_are_not_symbols_but_their_calls_belong_to_the_outer_symbol() -> None:
    r = parse_source("a.py", "python", SAMPLE)
    assert "inner" not in {s.name for s in r.parsed.symbols}
    owners = {(c, o) for c, _recv, o in calls(r.parsed)}
    assert ("deep", "Foo.amethod") in owners
    assert ("inner", "Foo.amethod") in owners


def test_parents_and_signatures() -> None:
    r = parse_source("a.py", "python", SAMPLE)
    foo = sym_by_name(r.parsed, "Foo")
    method = sym_by_name(r.parsed, "method")
    assert r.parsed.symbols[method.parent or 0] == foo
    assert foo.signature == "class Foo(Base):"  # the class line, not the decorator
    assert sym_by_name(r.parsed, "amethod").signature == "async def amethod():"
    assert sym_by_name(r.parsed, "helper").signature == "def helper(x):"


def test_imports_in_every_form() -> None:
    r = parse_source("pkg/mod.py", "python", SAMPLE)
    got = [
        (
            i.name,
            i.detail["level"],
            [(n["name"], n["alias"]) for n in i.detail["names"]],
            i.detail.get("alias"),
        )
        for i in imports(r.parsed)
    ]
    assert got == [
        ("os", 0, [], None),
        ("sys", 0, [], "system"),
        (".pkg", 1, [("a", None), ("b", "c")], None),
        (".", 1, [("x", None)], None),
        ("..up.deep", 2, [("*", None)], None),
        ("collections", 0, [("OrderedDict", None)], None),
        ("fast", 0, [], None),
    ]
    assert [i.line for i in imports(r.parsed)][:3] == [2, 2, 3]


def test_calls_are_deduplicated_with_a_count_and_keep_their_receiver() -> None:
    r = parse_source("a.py", "python", SAMPLE)
    by_key = {
        (c.name, c.detail["receiver"], c.symbol): c for c in r.parsed.refs if c.kind == "call"
    }
    method_idx = next(i for i, s in enumerate(r.parsed.symbols) if s.name == "method")
    assert by_key[("helper", None, method_idx)].detail["count"] == 2
    assert by_key[("helper", "self", method_idx)].detail["count"] == 1
    assert ("join", "os.path", 0) in by_key
    # Calls on an expression keep the method name but never invent a receiver.
    assert any(c.name == "method" and c.detail["receiver"] == "<expr>" for c in by_key.values())


def test_module_level_calls_have_no_symbol() -> None:
    r = parse_source("a.py", "python", "setup()\nobj.run()\n")
    assert calls(r.parsed) == [("setup", None, None), ("run", "obj", None)]


def test_empty_and_whitespace_files() -> None:
    for content in ["", "\n", "   \n\n  \n"]:
        r = parse_ok("a.py", "python", content)
        assert r.parsed.symbols == [] and r.chunks == []


def test_syntax_errors_are_flagged_but_everything_recoverable_is_extracted() -> None:
    broken = (
        "def good():\n    return 1\n\n"
        "def broken(:\n    pass\n\n"
        "class Fine:\n    def ok(self): ...\n"
    )
    r = parse_ok("a.py", "python", broken)
    assert r.parsed.syntax_errors is True
    names = {s.name for s in r.parsed.symbols}
    assert {"good", "Fine", "ok"} <= names


def test_unicode_identifiers_and_non_ascii_text_keep_line_numbers_right() -> None:
    code = '# héllo wörld 日本語\ndef 関数():\n    return "☃"\n\nclass Ünïcode:\n    pass\n'
    r = parse_ok("a.py", "python", code)
    assert symbols(r.parsed) == [("function", "関数", 1, 3), ("class", "Ünïcode", 5, 6)]


def test_decorated_class_range_starts_at_the_first_decorator_and_comments_attach() -> None:
    code = "x = 1\n\n# note\n@a\n@b\nclass C:\n    pass\n"
    r = parse_ok("a.py", "python", code)
    assert symbols(r.parsed) == [("class", "C", 3, 7)]
