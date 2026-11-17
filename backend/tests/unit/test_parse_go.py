from app.services.parsing.parse import parse_source
from tests.parse_helpers import calls, imports, parse_ok, qualified, symbols

GO = """package main

import (
	"fmt"
	str "strings"
	_ "embed"
	. "math"
	"github.com/x/y/z"
)

import "os"

// Server holds state.
type Server struct {
	Addr string
}

type Handler interface {
	Serve() error
}

type ID int

type (
	A struct{}
	B = string
)

// Start runs the server.
func (s *Server) Start(a int) error {
	fmt.Println("x")
	s.helper()
	s.helper()
	return os.Exit(1)
}

func (s Server) Value() int { return 1 }

func New() *Server { return &Server{} }

func generic[T any](x T) T { return x }

const Max = 1
var global = New()
"""


def test_go_symbols_and_ranges() -> None:
    r = parse_ok("cmd/main.go", "go", GO)
    assert symbols(r.parsed) == [
        ("struct", "Server", 13, 16),  # includes the doc comment and the `type` keyword
        ("interface", "Handler", 18, 20),
        ("type", "ID", 22, 22),
        ("struct", "A", 25, 25),
        ("type", "B", 26, 26),
        ("method", "Start", 29, 35),  # includes its doc comment
        ("method", "Value", 37, 37),
        ("function", "New", 39, 39),
        ("function", "generic", 41, 41),
    ]
    assert qualified(r.parsed) == [
        "Server", "Handler", "ID", "A", "B", "Server.Start", "Server.Value", "New", "generic",
    ]  # fmt: skip


def test_go_imports_in_every_form() -> None:
    r = parse_source("main.go", "go", GO)
    got = [
        (i.name, i.detail["alias"], i.detail["blank"], i.detail["dot"]) for i in imports(r.parsed)
    ]
    assert got == [
        ("fmt", None, False, False),
        ("strings", "str", False, False),
        ("embed", None, True, False),
        ("math", None, False, True),
        ("github.com/x/y/z", None, False, False),
        ("os", None, False, False),
    ]


def test_go_calls_have_receivers_and_owners() -> None:
    r = parse_source("main.go", "go", GO)
    got = set(calls(r.parsed))
    assert ("Println", "fmt", "Server.Start") in got
    assert ("helper", "s", "Server.Start") in got
    assert ("Exit", "os", "Server.Start") in got
    assert ("New", None, None) in got  # package-level var initializer
    counts = {
        (c.name, c.detail["receiver"]): c.detail["count"] for c in r.parsed.refs if c.kind == "call"
    }
    assert counts[("helper", "s")] == 2


def test_go_methods_on_embedded_generic_and_pointer_receivers() -> None:
    code = (
        "package p\n\ntype Box[T any] struct{}\n\nfunc (b *Box[T]) Get() T { var z T; return z }\n"
    )
    r = parse_ok("p.go", "go", code)
    assert qualified(r.parsed) == ["Box", "Box.Get"]


def test_go_syntax_error_is_flagged() -> None:
    code = "package p\n\nfunc good() {}\n\nfunc broken( {\n}\n\nfunc alsoGood() {}\n"
    r = parse_ok("p.go", "go", code)
    assert r.parsed.syntax_errors is True
    assert {"good", "alsoGood"} <= {s.name for s in r.parsed.symbols}
