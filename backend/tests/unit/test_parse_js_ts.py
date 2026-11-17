from app.services.parsing.parse import parse_source
from tests.parse_helpers import calls, imports, parse_ok, qualified, symbols

JS = """import def, { a, b as c } from "./mod";
import * as ns from "pkg";
import "side-effect";
const fs = require("fs");
const { x, y } = require("./xy");
export { x } from "./re";
export * from "./all";

/** Adds things. */
export function top(a) {
  helper(a);
  obj.method();
  new Thing();
  new ns.Other();
}

export default class K extends B {
  static s() {}
  m() { this.n(); }
  get g() { return 1; }
  handler = () => { go(); };
}

const arrow = (a) => a + 1;
const fe = function named() {};
const lazy = import("./lazy");
exports.util = function () { return 1; };
Foo.prototype.bar = function () { baz(); };
module.exports.other = () => {};
"""


def test_javascript_symbols_and_ranges() -> None:
    r = parse_ok("src/a.js", "javascript", JS)
    assert symbols(r.parsed) == [
        ("function", "top", 9, 15),  # includes the doc comment and the export keyword
        ("class", "K", 17, 22),
        ("method", "s", 18, 18),
        ("method", "m", 19, 19),
        ("method", "g", 20, 20),
        ("method", "handler", 21, 21),  # a class field holding an arrow function
        ("function", "arrow", 24, 24),
        ("function", "fe", 25, 25),
        ("function", "util", 27, 27),
        ("method", "bar", 28, 28),
        ("function", "other", 29, 29),
    ]
    assert qualified(r.parsed)[:5] == ["top", "K", "K.s", "K.m", "K.g"]
    assert qualified(r.parsed)[-2] == "Foo.bar"


def test_javascript_imports_in_every_form() -> None:
    r = parse_source("a.js", "javascript", JS)
    got = {i.name: i.detail for i in imports(r.parsed)}
    assert got["./mod"]["default"] == "def"
    assert [(n["name"], n["alias"]) for n in got["./mod"]["names"]] == [("a", None), ("b", "c")]
    assert got["pkg"]["namespace"] == "ns"
    assert got["side-effect"]["names"] == [] and got["side-effect"]["default"] is None
    assert got["fs"] == {"kind": "require", "binding": "fs", "names": []}
    assert got["./xy"]["binding"] == "{ x, y }"
    assert got["./re"]["kind"] == "reexport" and got["./re"]["names"][0]["name"] == "x"
    assert got["./all"]["kind"] == "reexport" and got["./all"]["star"] is True
    assert got["./lazy"]["kind"] == "dynamic"


def test_require_and_dynamic_import_are_imports_not_calls() -> None:
    r = parse_source("a.js", "javascript", JS)
    callees = {c for c, _r, _o in calls(r.parsed)}
    assert "require" not in callees and "import" not in callees


def test_javascript_calls_and_new_expressions() -> None:
    r = parse_source("a.js", "javascript", JS)
    got = set(calls(r.parsed))
    assert ("helper", None, "top") in got
    assert ("method", "obj", "top") in got
    assert ("n", "this", "K.m") in got
    assert ("go", None, "K.handler") in got
    assert ("baz", None, "Foo.bar") in got
    news = {c.name for c in r.parsed.refs if c.kind == "call" and c.detail.get("new")}
    assert news == {"Thing", "Other"}


TS = """import type { T } from "./types";
import { type U, v } from "./u";

export interface Foo<T> extends Bar {
  a: string;
  m(): void;
}

export type Alias = { a: number };

export enum Color { Red, Green }

abstract class Abs {
  abstract f(): void;
  private g(x: number): string { return ""; }
}

export const typed = async (x: number): Promise<void> => {
  await go(x);
};

declare function ambient(a: string): void;

namespace NS {
  export function inner() {}
}

export default function () { return 1; }
"""


def test_typescript_symbols() -> None:
    r = parse_ok("src/a.ts", "typescript", TS)
    assert symbols(r.parsed) == [
        ("interface", "Foo", 4, 7),
        ("type", "Alias", 9, 9),
        ("enum", "Color", 11, 11),
        ("class", "Abs", 13, 16),
        ("method", "g", 15, 15),  # abstract signatures are not symbols
        ("function", "typed", 18, 20),
        ("function", "ambient", 22, 22),
        ("namespace", "NS", 24, 26),
        ("function", "inner", 25, 25),
        ("function", "default", 28, 28),
    ]
    assert qualified(r.parsed)[4] == "Abs.g" and qualified(r.parsed)[8] == "NS.inner"


def test_typescript_type_only_imports_are_marked() -> None:
    r = parse_source("a.ts", "typescript", TS)
    types, u = imports(r.parsed)
    assert types.detail["type_only"] is True
    assert u.detail["type_only"] is False
    assert [(n["name"], n["type_only"]) for n in u.detail["names"]] == [("U", True), ("v", False)]
    assert ("go", None, "typed") in set(calls(r.parsed))


def test_tsx_uses_the_tsx_grammar() -> None:
    code = (
        'import React from "react";\n'
        "export function Button({ label }: { label: string }) {\n"
        "  return <button onClick={() => track(label)}>{label}</button>;\n"
        "}\n"
    )
    r = parse_ok("src/c.tsx", "typescript", code)
    assert r.parsed.syntax_errors is False
    assert symbols(r.parsed) == [("function", "Button", 2, 4)]
    assert ("track", None, "Button") in set(calls(r.parsed))


def test_jsx_in_a_js_file() -> None:
    code = "export const App = () => <div>{render()}</div>;\n"
    r = parse_ok("src/app.jsx", "javascript", code)
    assert r.parsed.syntax_errors is False
    assert ("render", None, "App") in set(calls(r.parsed))


def test_class_with_string_and_computed_method_names() -> None:
    code = "class C {\n  'quoted'() {}\n  [Symbol.iterator]() {}\n  #private() {}\n}\n"
    r = parse_ok("a.js", "javascript", code)
    assert [s.name for s in r.parsed.symbols] == ["C", "quoted", "[Symbol.iterator]", "#private"]


def test_syntax_error_in_javascript_still_yields_the_good_parts() -> None:
    code = "function good() { return 1; }\nfunction broken( {\n}\nconst fine = () => 2;\n"
    r = parse_ok("a.js", "javascript", code)
    assert r.parsed.syntax_errors is True
    # Tree-sitter's error recovery may swallow declarations after the error, so only what comes
    # before it is guaranteed.
    assert "good" in {s.name for s in r.parsed.symbols}
