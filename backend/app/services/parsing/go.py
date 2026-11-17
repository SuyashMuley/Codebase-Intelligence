"""Go extractor: functions, methods, struct/interface/type declarations, imports, call sites."""

from collections.abc import Callable

from tree_sitter import Node

from app.services.parsing.model import FUNCTION, INTERFACE, METHOD, STRUCT, TYPE, ParsedFile, Symbol
from app.services.parsing.refs import RefCollector
from app.services.parsing.util import (
    MAX_NODES,
    attach_leading_comments,
    line_range,
    node_text,
    signature_of,
)

_COMMENT_PREFIXES = ("//", "/*", "*", "*/")


class _Extractor:
    def __init__(self, root: Node, src: bytes, lines: list[str], path: str) -> None:
        self.root, self.src, self.lines, self.path = root, src, lines, path
        self.symbols: list[Symbol] = []
        self.node_symbol: dict[int, int] = {}
        self.refs = RefCollector()

    def _add(self, kind: str, name: str, outer: Node, owner: Node, prefix: str = "") -> None:
        start, end = line_range(outer)
        start = attach_leading_comments(self.lines, start, 1, _COMMENT_PREFIXES)
        self.symbols.append(
            Symbol(
                kind=kind,
                name=name,
                qualified_name=f"{self.path}::{prefix}{name}",
                start_line=start,
                end_line=end,
                parent=None,  # Go declarations are never nested, and methods live outside types
                signature=signature_of(self.lines, owner.start_point[0] + 1),
            )
        )
        self.node_symbol[owner.id] = len(self.symbols) - 1

    def _receiver_type(self, method: Node) -> str:
        receiver = method.child_by_field_name("receiver")
        if receiver is None:
            return ""
        for decl in receiver.named_children:
            node = decl.child_by_field_name("type")
            while node is not None and node.type in (
                "pointer_type",
                "generic_type",
                "parenthesized_type",
            ):
                inner = node.child_by_field_name("type") or (
                    node.named_children[0] if node.named_children else None
                )
                node = inner
            if node is not None:
                return node_text(node, self.src)
        return ""

    def _declarations(self) -> None:
        for node in self.root.named_children:
            if node.type == "function_declaration":
                name = node_text(node.child_by_field_name("name"), self.src)
                if name:
                    self._add(FUNCTION, name, node, node)
            elif node.type == "method_declaration":
                name = node_text(node.child_by_field_name("name"), self.src)
                recv = self._receiver_type(node)
                if name:
                    self._add(METHOD, name, node, node, f"{recv}." if recv else "")
            elif node.type == "type_declaration":
                specs = [c for c in node.named_children if c.type in ("type_spec", "type_alias")]
                for spec in specs:
                    name = node_text(spec.child_by_field_name("name"), self.src)
                    if not name:
                        continue
                    type_node = spec.child_by_field_name("type")
                    kind = {"struct_type": STRUCT, "interface_type": INTERFACE}.get(
                        type_node.type if type_node is not None else "", TYPE
                    )
                    # A one-spec declaration spans the `type` keyword too.
                    outer = node if len(specs) == 1 else spec
                    self._add(kind, name, outer, spec)

    # --- references --------------------------------------------------------------------

    def _import_spec(self, node: Node, sym: int | None) -> None:
        path_node = node.child_by_field_name("path")
        path = ""
        if path_node is not None:
            content = next((c for c in path_node.named_children if "content" in c.type), None)
            path = node_text(content, self.src) if content is not None else ""
        name = node.child_by_field_name("name")
        alias = node_text(name, self.src) if name is not None else None
        self.refs.add_import(
            path,
            node.start_point[0] + 1,
            sym,
            {
                "alias": alias if alias not in (None, "_", ".") else None,
                "blank": alias == "_",
                "dot": alias == ".",
            },
        )

    def _call(self, node: Node, sym: int | None) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        line = node.start_point[0] + 1
        if fn.type == "identifier":
            self.refs.add_call(node_text(fn, self.src), None, line, sym)
        elif fn.type == "selector_expression":
            name = node_text(fn.child_by_field_name("field"), self.src)
            operand = fn.child_by_field_name("operand")
            if operand is not None and operand.type in ("identifier", "selector_expression"):
                receiver: str | None = node_text(operand, self.src)[:80]
            else:
                receiver = "<expr>"
            self.refs.add_call(name, receiver, line, sym)

    def _collect(self) -> bool:
        handlers: dict[str, Callable[[Node, int | None], None]] = {
            "import_spec": self._import_spec,
            "call_expression": self._call,
        }
        stack: list[tuple[Node, int | None]] = [(self.root, None)]
        visited = 0
        while stack:
            node, current = stack.pop()
            visited += 1
            if visited > MAX_NODES:
                return True
            current = self.node_symbol.get(node.id, current)
            handler = handlers.get(node.type)
            if handler is not None:
                handler(node, current)
            for child in reversed(node.named_children):
                stack.append((child, current))
        return False

    def run(self) -> ParsedFile:
        self._declarations()
        truncated = self._collect()
        return ParsedFile(
            symbols=self.symbols,
            refs=self.refs.refs,
            syntax_errors=self.root.has_error,
            truncated=truncated or self.refs.truncated,
        )


def extract(root: Node, src: bytes, lines: list[str], path: str) -> ParsedFile:
    return _Extractor(root, src, lines, path).run()
