from tests.graph_helpers import build_graph

# --- Python ---------------------------------------------------------------------------------


def test_python_absolute_and_relative_imports_resolve_to_files() -> None:
    g = build_graph(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import b\nfrom .c import thing\nimport pkg.d\n",
            "pkg/b.py": "",
            "pkg/c.py": "def thing(): pass\n",
            "pkg/d.py": "",
        }
    )
    assert g.imports() == {
        ("pkg/a.py", "pkg/__init__.py"),
        ("pkg/a.py", "pkg/b.py"),
        ("pkg/a.py", "pkg/c.py"),
        ("pkg/a.py", "pkg/d.py"),
    }


def test_standard_library_and_third_party_imports_are_unresolved_not_dropped() -> None:
    g = build_graph({"a.py": "import os\nimport requests\nfrom flask import Flask\n"})
    assert g.imports() == set()
    assert g.imports(static=False) == {("a.py", "os"), ("a.py", "requests"), ("a.py", "flask")}
    assert set(g.reasons().values()) == {"external_or_unresolved"}


def test_src_layout_resolves_against_the_package_root() -> None:
    g = build_graph(
        {
            "src/lib/__init__.py": "",
            "src/lib/core.py": "def run(): pass\n",
            "tests/test_core.py": "from lib.core import run\n\ndef test_it():\n    run()\n",
        }
    )
    assert ("tests/test_core.py", "src/lib/core.py") in g.imports()
    assert g.calls() == {("tests/test_core.py::test_it", "src/lib/core.py::run")}


def test_a_name_matching_two_package_roots_is_not_guessed() -> None:
    g = build_graph(
        {
            "one/util.py": "def f(): pass\n",
            "two/util.py": "def f(): pass\n",
            "three/main.py": "import util\n\ndef go():\n    util.f()\n",
        }
    )
    assert g.imports() == set()  # ambiguous: could be either
    assert g.calls() == set()


def test_python_calls_resolve_through_imports_and_same_module_names() -> None:
    g = build_graph(
        {
            "app/__init__.py": "",
            "app/util.py": "def slug(s): return s\n\nclass Box:\n    def open(self): pass\n",
            "app/main.py": (
                "from app.util import slug, Box as B\n"
                "import app.util as u\n\n"
                "def local(): pass\n\n"
                "def run():\n"
                "    slug('x')\n"
                "    local()\n"
                "    u.slug('y')\n"
                "    B()\n"
                "    B.open(None)\n"
            ),
        }
    )
    run = "app/main.py::run"
    assert g.calls() == {
        (run, "app/util.py::slug"),
        (run, "app/main.py::local"),
        (run, "app/util.py::Box"),
        (run, "app/util.py::Box.open"),
    }


def test_self_calls_resolve_to_methods_of_the_same_class_only() -> None:
    g = build_graph(
        {
            "a.py": (
                "class A:\n"
                "    def one(self):\n"
                "        self.two()\n"
                "        self.inherited()\n\n"
                "    def two(self): pass\n\n"
                "class B:\n"
                "    def two(self): pass\n"
            )
        }
    )
    assert g.calls() == {("a.py::A.one", "a.py::A.two")}
    assert g.reasons()["self.inherited"] == "inherited_or_dynamic"


def test_calls_on_variables_are_never_guessed_even_when_the_method_name_is_unique() -> None:
    g = build_graph(
        {
            "svc.py": "class Service:\n    def charge(self): pass\n",
            "use.py": (
                "from svc import Service\n\n"
                "def go(client):\n"
                "    client.charge()\n"
                "    Service().charge()\n"
                "    make()().charge()\n"
            ),
        }
    )
    assert ("use.py::go", "svc.py::Service.charge") not in g.calls()
    reasons = g.reasons()
    assert reasons["client.charge"] == "dynamic_receiver"
    assert reasons["<expr>.charge"] == "dynamic_receiver"


def test_reexports_through_an_init_file_are_followed() -> None:
    g = build_graph(
        {
            "pkg/__init__.py": "from .api import get\n",
            "pkg/api.py": "def get(): pass\n",
            "use.py": "from pkg import get\n\ndef go():\n    get()\n",
        }
    )
    assert g.calls() == {("use.py::go", "pkg/api.py::get")}
    assert ("use.py", "pkg/__init__.py") in g.imports()


def test_from_package_import_submodule_is_a_module_not_a_symbol() -> None:
    g = build_graph(
        {
            "pkg/__init__.py": "",
            "pkg/helpers.py": "def h(): pass\n",
            "use.py": "from pkg import helpers\n\ndef go():\n    helpers.h()\n",
        }
    )
    assert g.calls() == {("use.py::go", "pkg/helpers.py::h")}
    assert ("use.py", "pkg/helpers.py") in g.imports()


def test_a_name_both_defined_and_imported_is_ambiguous() -> None:
    g = build_graph(
        {
            "lib.py": "def f(): pass\n",
            "a.py": "from lib import f\n\ndef f(): pass\n\ndef go():\n    f()\n",
        }
    )
    assert g.calls() == set()


def test_python_builtins_and_unknown_names_are_unresolved() -> None:
    g = build_graph({"a.py": "def go():\n    print(len([]))\n"})
    assert g.calls() == set()
    assert {e.reason for e in g.unresolved_calls()} == {"not_found"}


def test_module_level_calls_have_no_caller_symbol_but_a_caller_file() -> None:
    g = build_graph({"a.py": "def f(): pass\n\nf()\n"})
    assert g.calls() == {("a.py", "a.py::f")}


def test_repeated_calls_are_counted_not_duplicated() -> None:
    g = build_graph({"a.py": "def f(): pass\n\ndef go():\n    f()\n    f()\n    f()\n"})
    edges = [e for e in g.result.edges if e.kind == "calls"]
    assert len(edges) == 1 and edges[0].count == 3


def test_import_cycles_do_not_hang_the_export_walk() -> None:
    g = build_graph(
        {
            "a.py": "from b import x\n",
            "b.py": "from a import x\n",
            "c.py": "from a import x\n\ndef go():\n    x()\n",
        }
    )
    assert g.calls() == set()


# --- JavaScript and TypeScript -------------------------------------------------------------


def test_js_relative_imports_find_files_with_extensions_and_index_files() -> None:
    g = build_graph(
        {
            "src/main.ts": (
                "import { a } from './a';\n"
                "import b from './b.js';\n"
                "import * as u from './util';\n"
                "import { x } from 'react';\n"
                "import { y } from '@/alias/y';\n"
            ),
            "src/a.ts": "export function a() {}\n",
            "src/b.ts": "export default function b() {}\n",
            "src/util/index.ts": "export function u() {}\n",
        }
    )
    assert g.imports() == {
        ("src/main.ts", "src/a.ts"),
        ("src/main.ts", "src/b.ts"),
        ("src/main.ts", "src/util/index.ts"),
    }
    assert g.imports(static=False) == {("src/main.ts", "react"), ("src/main.ts", "@/alias/y")}


def test_js_calls_resolve_through_named_namespace_and_require_bindings() -> None:
    g = build_graph(
        {
            "lib.js": "function a() {}\nfunction b() {}\nmodule.exports = { a, b };\n",
            "use.js": (
                "import { a as first } from './lib';\n"
                "const lib = require('./lib');\n"
                "const { b } = require('./lib');\n\n"
                "function go() {\n  first();\n  lib.b();\n  b();\n}\n"
            ),
        }
    )
    callee_a, callee_b = "lib.js::a", "lib.js::b"
    assert g.calls() == {("use.js::go", callee_a), ("use.js::go", callee_b)}


def test_default_imports_link_the_file_but_not_a_guessed_symbol() -> None:
    g = build_graph(
        {
            "thing.ts": "export default function thing() {}\n",
            "use.ts": "import t from './thing';\n\nexport function go() { t(); }\n",
        }
    )
    assert ("use.ts", "thing.ts") in g.imports()
    assert g.calls() == set()


def test_js_this_calls_and_reexports() -> None:
    g = build_graph(
        {
            "index.ts": "export { run } from './impl';\nexport * from './more';\n",
            "impl.ts": "export function run() {}\n",
            "more.ts": "export function extra() {}\n",
            "use.ts": (
                "import { run, extra } from './index';\n\n"
                "export function go() { run(); extra(); }\n\n"
                "export class K {\n  a() { this.b(); }\n  b() {}\n}\n"
            ),
        }
    )
    assert g.calls() == {
        ("use.ts::go", "impl.ts::run"),
        ("use.ts::go", "more.ts::extra"),
        ("use.ts::K.a", "use.ts::K.b"),
    }


def test_js_dynamic_import_with_a_literal_is_static_and_a_computed_one_is_not_recorded() -> None:
    g = build_graph(
        {
            "a.ts": (
                "export async function go(x: string) {\n"
                "  await import('./b');\n"
                "  await import('./' + x);\n}\n"
            ),
            "b.ts": "",
        }
    )
    assert g.imports() == {("a.ts", "b.ts")}
    assert g.imports(static=False) == set()  # nothing invented for the computed specifier


def test_a_directory_import_of_the_repository_root_finds_its_index_file() -> None:
    g = build_graph({"index.js": "exports.x = 1;\n", "test/a.js": "const app = require('..');\n"})
    assert g.imports() == {("test/a.js", "index.js")}


def test_imports_outside_the_repository_root_are_unresolved() -> None:
    g = build_graph({"a.ts": "import x from '../../outside';\n"})
    assert g.imports() == set()


# --- Go -------------------------------------------------------------------------------------


GO_FILES = {
    "main.go": (
        'package main\n\nimport (\n\t"fmt"\n\t"github.com/o/r/util"\n\tstr "github.com/o/r/strs"\n'
        '\t"github.com/someone/else/util"\n)\n\n'
        "func main() {\n\thelper()\n\tutil.Do()\n\tstr.Up()\n\tfmt.Println()\n\ts.Method()\n}\n"
    ),
    "helper.go": "package main\n\nfunc helper() {}\n",
    "util/util.go": "package util\n\nfunc Do() {}\n",
    "strs/s.go": "package strs\n\nfunc Up() {}\n",
}


def test_go_same_package_and_package_qualified_calls_resolve() -> None:
    g = build_graph(GO_FILES)
    assert g.calls() == {
        ("main.go::main", "helper.go::helper"),
        ("main.go::main", "util/util.go::Do"),
        ("main.go::main", "strs/s.go::Up"),
    }


def test_go_imports_resolve_only_inside_this_repository() -> None:
    g = build_graph(GO_FILES)
    static = g.imports()
    assert ("main.go", "util/util.go") in static and ("main.go", "strs/s.go") in static
    unresolved = g.imports(static=False)
    assert ("main.go", "fmt") in unresolved
    assert ("main.go", "github.com/someone/else/util") in unresolved


def test_go_method_calls_need_a_type_so_they_stay_unresolved() -> None:
    g = build_graph(GO_FILES)
    assert g.reasons()["s.Method"] == "dynamic_receiver"


def test_go_test_files_are_not_import_targets() -> None:
    g = build_graph(
        {
            "main.go": 'package main\n\nimport "github.com/o/r/pkg"\n\nfunc main() { pkg.A() }\n',
            "pkg/a.go": "package pkg\n\nfunc A() {}\n",
            "pkg/a_test.go": "package pkg\n\nfunc TestA() {}\n",
        }
    )
    assert g.imports() == {("main.go", "pkg/a.go")}


# --- general ---------------------------------------------------------------------------------


def test_every_static_edge_points_at_real_files_and_symbols() -> None:
    g = build_graph(GO_FILES)
    for e in g.result.edges:
        if e.resolution == "static":
            assert e.dst_file_id is not None
            if e.kind == "calls":
                assert e.dst_symbol_id is not None
        else:
            assert e.dst_file_id is None and e.dst_symbol_id is None and e.reason


def test_the_graph_is_deterministic() -> None:
    a = build_graph(GO_FILES).result.edges
    b = build_graph(GO_FILES).result.edges
    assert a == b


def test_two_definitions_of_one_name_are_ambiguous_not_guessed() -> None:
    g = build_graph(
        {
            "a.py": (
                "if True:\n    def f(): pass\nelse:\n    def f(): pass\n\ndef go():\n    f()\n"
            ),
            "b.py": "from a import f\n\ndef use():\n    f()\n",
        }
    )
    assert g.calls() == set()
    assert {e.reason for e in g.unresolved_calls()} == {"not_found"}


def test_self_calls_use_the_class_that_contains_the_caller() -> None:
    g = build_graph(
        {
            "a.py": (
                "class A:\n    def two(self): pass\n\n"
                "class B:\n    def one(self):\n        self.two()\n\n    def two(self): pass\n"
            )
        }
    )
    assert g.calls() == {("a.py::B.one", "a.py::B.two")}


def test_several_imports_of_one_file_become_one_edge_with_a_count() -> None:
    g = build_graph(
        {
            "lib.py": "def f(): pass\n\ndef g(): pass\n",
            "a.py": "from lib import f\nfrom lib import g\nimport lib\n",
        }
    )
    edges = [e for e in g.result.edges if e.kind == "imports" and e.resolution == "static"]
    assert len(edges) == 1 and edges[0].count == 3 and edges[0].line == 1


# --- member assignments are not names in scope (found by auditing real edges on express) --------


def test_a_bare_call_does_not_resolve_to_a_property_assignment_of_the_same_name() -> None:
    g = build_graph(
        {
            "lib/request.js": (
                "var accepts = require('accepts');\n"
                "var req = Object.create(proto);\n\n"
                "req.accepts = function(){\n"
                "  var accept = accepts(this);\n"
                "  return accept;\n"
                "};\n\n"
                "req.acceptsCharsets = function(){\n"
                "  var accept = accepts(this);\n"
                "  return accept;\n"
                "};\n"
            )
        }
    )
    assert g.calls() == set()  # `accepts(...)` is the npm package, not `req.accepts`
    assert {e.target_name for e in g.unresolved_calls()} >= {"accepts"}


def test_a_named_function_expression_assigned_to_a_member_is_not_in_scope_either() -> None:
    g = build_graph(
        {
            "lib/response.js": (
                "var send = require('send');\n"
                "var res = {};\n\n"
                "res.send = function send(body) { return body; };\n\n"
                "res.sendFile = function sendFile(path) {\n"
                "  return send(path);\n"
                "};\n"
            )
        }
    )
    assert g.calls() == set()


def test_exports_assignments_are_importable_but_not_names_in_scope() -> None:
    g = build_graph(
        {
            "lib.js": (
                "exports.helper = function helper() { return 1; };\n\n"
                "exports.other = function other() { return helper(); };\n"
            ),
            "use.js": "const { helper } = require('./lib');\n\nfunction go() { helper(); }\n",
        }
    )
    assert g.calls() == {("use.js::go", "lib.js::helper")}  # importable...
    assert ("lib.js::other", "lib.js::helper") not in g.calls()  # ...but not a local name


def test_ordinary_declarations_are_still_local_names() -> None:
    g = build_graph(
        {"a.js": "function f() {}\nconst g = function () {};\nfunction go() { f(); g(); }\n"}
    )
    assert g.calls() == {("a.js::go", "a.js::f"), ("a.js::go", "a.js::g")}
