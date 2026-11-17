"""JavaScript and TypeScript extractor (one grammar family, shared node types)."""

from collections.abc import Callable
from typing import Any

from tree_sitter import Node

from app.services.parsing.model import (
    CLASS,
    ENUM,
    FUNCTION,
    INTERFACE,
    METHOD,
    NAMESPACE,
    TYPE,
    ParsedFile,
    Symbol,
)
from app.services.parsing.refs import RefCollector
from app.services.parsing.util import (
    MAX_DEPTH,
    MAX_NODES,
    attach_leading_comments,
    line_range,
    node_text,
    signature_of,
)

_FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_FUNC_DECLS = {"function_declaration", "generator_function_declaration", "function_signature"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
_MODULES = {"internal_module", "module"}
_COMMENT_PREFIXES = ("//", "/*", "*", "*/")


def _string_value(node: Node | None, src: bytes) -> str | None:
    """The text of a plain string literal node, or None for anything else (templates, etc.)."""
    if node is None or node.type != "string":
        return None
    for child in node.named_children:
        if child.type == "string_fragment":
            return node_text(child, src)
    return "" if node.named_child_count == 0 else None


class _Extractor:
    def __init__(self, root: Node, src: bytes, lines: list[str], path: str) -> None:
        self.root, self.src, self.lines, self.path = root, src, lines, path
        self.symbols: list[Symbol] = []
        self.node_symbol: dict[int, int] = {}
        self.refs = RefCollector()

    # --- definitions -------------------------------------------------------------------

    def _add(
        self,
        kind: str,
        name: str,
        outer: Node,
        owner: Node,
        parent: int | None,
        prefix: str,
        map_node: Node | None = None,
    ) -> int:
        start, end = line_range(outer)
        floor = self.symbols[parent].start_line + 1 if parent is not None else 1
        start = attach_leading_comments(self.lines, start, floor, _COMMENT_PREFIXES)
        self.symbols.append(
            Symbol(
                kind=kind,
                name=name,
                qualified_name=f"{self.path}::{prefix}{name}",
                start_line=start,
                end_line=end,
                parent=parent,
                signature=signature_of(self.lines, owner.start_point[0] + 1),
            )
        )
        idx = len(self.symbols) - 1
        self.node_symbol[(map_node or owner).id] = idx
        return idx

    def _member_name(self, node: Node | None) -> str:
        if node is None:
            return ""
        if node.type == "string":
            return _string_value(node, self.src) or ""
        return node_text(node, self.src)

    def _class_members(self, cls: Node, idx: int, prefix: str) -> None:
        body = cls.child_by_field_name("body")
        if body is None:
            return
        for member in body.named_children:
            if member.type == "method_definition":
                name = self._member_name(member.child_by_field_name("name"))
                if name:
                    self._add(METHOD, name, member, member, idx, prefix)
            elif member.type in ("public_field_definition", "field_definition"):
                value = member.child_by_field_name("value")
                name = self._member_name(
                    member.child_by_field_name("name") or member.child_by_field_name("property")
                )
                if value is not None and value.type in _FUNC_VALUES and name:
                    self._add(METHOD, name, member, member, idx, prefix, map_node=value)

    def _statements(self, container: Node, parent: int | None, prefix: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for child in container.named_children:
            self._statement(child, parent, prefix, depth)

    def _statement(self, node: Node, parent: int | None, prefix: str, depth: int) -> None:
        outer = node
        if node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is None:
                value = node.child_by_field_name("value")  # export default <expression>
                if value is not None and value.type in _FUNC_VALUES | {"class"}:
                    kind = CLASS if value.type == "class" else FUNCTION
                    idx = self._add(kind, "default", outer, value, parent, prefix)
                    if kind == CLASS:
                        self._class_members(value, idx, f"{prefix}default.")
                return
            node = decl
        if node.type == "ambient_declaration" and node.named_children:
            node = node.named_children[0]

        t = node.type
        name_node = node.child_by_field_name("name")
        name = node_text(name_node, self.src)
        if t in _FUNC_DECLS and name:
            self._add(FUNCTION, name, outer, node, parent, prefix)
        elif t in _CLASS_DECLS or (t == "class" and name):
            if name:
                idx = self._add(CLASS, name, outer, node, parent, prefix)
                self._class_members(node, idx, f"{prefix}{name}.")
        elif t in ("lexical_declaration", "variable_declaration"):
            self._declarators(node, outer, parent, prefix)
        elif t == "interface_declaration" and name:
            self._add(INTERFACE, name, outer, node, parent, prefix)
        elif t == "type_alias_declaration" and name:
            self._add(TYPE, name, outer, node, parent, prefix)
        elif t == "enum_declaration" and name:
            self._add(ENUM, name, outer, node, parent, prefix)
        elif t in _MODULES and name:
            self._namespace(node, outer, name, parent, prefix, depth)
        elif t == "expression_statement" and node.named_children:
            inner = node.named_children[0]
            if inner.type in _MODULES:
                self._namespace(
                    inner,
                    outer,
                    node_text(inner.child_by_field_name("name"), self.src),
                    parent,
                    prefix,
                    depth,
                )
            elif inner.type == "assignment_expression":
                self._assignment(inner, outer, parent, prefix)

    def _namespace(
        self, node: Node, outer: Node, name: str, parent: int | None, prefix: str, depth: int
    ) -> None:
        if not name:
            return
        idx = self._add(NAMESPACE, name, outer, node, parent, prefix)
        body = node.child_by_field_name("body")
        if body is not None:
            self._statements(body, idx, f"{prefix}{name}.", depth + 1)

    def _declarators(self, node: Node, outer: Node, parent: int | None, prefix: str) -> None:
        declarators = [c for c in node.named_children if c.type == "variable_declarator"]
        for d in declarators:
            name_node = d.child_by_field_name("name")
            value = d.child_by_field_name("value")
            if (
                name_node is not None
                and name_node.type == "identifier"
                and value is not None
                and value.type in _FUNC_VALUES
            ):
                span_node = outer if len(declarators) == 1 else d
                self._add(
                    FUNCTION,
                    node_text(name_node, self.src),
                    span_node,
                    d,
                    parent,
                    prefix,
                    map_node=value,
                )

    def _assignment(self, assign: Node, outer: Node, parent: int | None, prefix: str) -> None:
        """CommonJS style: `exports.f = function(){}`, `Foo.prototype.m = () => {}`."""
        left, right = assign.child_by_field_name("left"), assign.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            return
        if right.type not in _FUNC_VALUES:
            return
        obj = node_text(left.child_by_field_name("object"), self.src)
        prop = node_text(left.child_by_field_name("property"), self.src)
        if not prop or not obj:
            return
        if obj.endswith(".prototype"):
            self._add(METHOD, prop, outer, assign, parent, f"{obj[: -len('.prototype')]}.", right)
        elif obj in ("exports", "module.exports"):
            self._add(FUNCTION, prop, outer, assign, parent, prefix, right)
        else:
            self._add(FUNCTION, prop, outer, assign, parent, f"{obj}.", right)

    # --- references --------------------------------------------------------------------

    def _import_statement(self, node: Node, sym: int | None) -> None:
        source = _string_value(node.child_by_field_name("source"), self.src)
        if not source:
            return
        detail: dict[str, Any] = {
            "kind": "import",
            "default": None,
            "namespace": None,
            "names": [],
            "type_only": node_text(node, self.src).lstrip().startswith("import type"),
        }
        for child in node.named_children:
            if child.type != "import_clause":
                continue
            for part in child.named_children:
                if part.type == "identifier":
                    detail["default"] = node_text(part, self.src)
                elif part.type == "namespace_import":
                    ident = next((c for c in part.named_children if c.type == "identifier"), None)
                    detail["namespace"] = node_text(ident, self.src)
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        if spec.type == "import_specifier":
                            detail["names"].append(self._specifier(spec))
        self.refs.add_import(source, node.start_point[0] + 1, sym, detail)

    def _specifier(self, spec: Node) -> dict[str, Any]:
        alias = spec.child_by_field_name("alias")
        return {
            "name": node_text(spec.child_by_field_name("name"), self.src),
            "alias": node_text(alias, self.src) if alias is not None else None,
            "type_only": node_text(spec, self.src).lstrip().startswith("type "),
        }

    def _export_statement(self, node: Node, sym: int | None) -> None:
        source = _string_value(node.child_by_field_name("source"), self.src)
        if not source:
            return  # a plain export, not a re-export
        names: list[dict[str, Any]] = []
        star = False
        for child in node.named_children:
            if child.type == "export_clause":
                names = [
                    self._specifier(s) for s in child.named_children if s.type == "export_specifier"
                ]
            elif child.type in ("namespace_export", "*"):
                star = True
        if not names:
            star = True
        detail = {
            "kind": "reexport",
            "names": names,
            "star": star,
            "default": None,
            "namespace": None,
        }
        self.refs.add_import(source, node.start_point[0] + 1, sym, detail)

    def _call(self, node: Node, sym: int | None) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        line = node.start_point[0] + 1
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if fn.type == "import":  # dynamic import("x")
            spec = _string_value(first, self.src)
            if spec:
                self.refs.add_import(spec, line, sym, {"kind": "dynamic", "names": []})
            return
        if fn.type == "identifier" and node_text(fn, self.src) == "require":
            spec = _string_value(first, self.src)
            if spec:
                self.refs.add_import(
                    spec,
                    line,
                    sym,
                    {"kind": "require", "binding": self._binding(node), "names": []},
                )
            return
        if fn.type == "identifier":
            self.refs.add_call(node_text(fn, self.src), None, line, sym)
        elif fn.type == "member_expression":
            name = node_text(fn.child_by_field_name("property"), self.src)
            obj = fn.child_by_field_name("object")
            if obj is not None and obj.type in ("identifier", "member_expression", "this", "super"):
                receiver: str | None = node_text(obj, self.src)[:80]
            else:
                receiver = "<expr>"
            self.refs.add_call(name, receiver, line, sym)

    def _binding(self, call: Node) -> str | None:
        """For `const x = require(...)`, the name x (or the destructured names)."""
        parent = call.parent
        if parent is None or parent.type != "variable_declarator":
            return None
        target = parent.child_by_field_name("name")
        return node_text(target, self.src)[:120] if target is not None else None

    def _new(self, node: Node, sym: int | None) -> None:
        ctor = node.child_by_field_name("constructor")
        if ctor is None:
            return
        line = node.start_point[0] + 1
        if ctor.type == "identifier":
            self.refs.add_call(node_text(ctor, self.src), None, line, sym, {"new": True})
        elif ctor.type == "member_expression":
            name = node_text(ctor.child_by_field_name("property"), self.src)
            obj = ctor.child_by_field_name("object")
            receiver = node_text(obj, self.src)[:80] if obj is not None else None
            self.refs.add_call(name, receiver, line, sym, {"new": True})

    def _collect(self) -> bool:
        handlers: dict[str, Callable[[Node, int | None], None]] = {
            "import_statement": self._import_statement,
            "export_statement": self._export_statement,
            "call_expression": self._call,
            "new_expression": self._new,
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
        self._statements(self.root, None, "", 0)
        truncated = self._collect()
        return ParsedFile(
            symbols=self.symbols,
            refs=self.refs.refs,
            syntax_errors=self.root.has_error,
            truncated=truncated or self.refs.truncated,
        )


def extract(root: Node, src: bytes, lines: list[str], path: str) -> ParsedFile:
    return _Extractor(root, src, lines, path).run()
