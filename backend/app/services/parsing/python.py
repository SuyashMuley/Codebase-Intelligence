"""Python extractor: classes, functions, methods, imports, and call sites."""

from collections.abc import Callable

from tree_sitter import Node

from app.services.parsing.model import CLASS, FUNCTION, METHOD, ParsedFile, Symbol
from app.services.parsing.refs import RefCollector
from app.services.parsing.util import (
    MAX_DEPTH,
    MAX_NODES,
    attach_leading_comments,
    line_range,
    node_text,
    signature_of,
)

# Statements whose bodies can hold conditional definitions (if TYPE_CHECKING:, try/except
# ImportError fallbacks, ...). Function bodies are deliberately not searched for definitions.
_COMPOUND = {
    "if_statement", "elif_clause", "else_clause", "try_statement", "except_clause",
    "except_group_clause", "finally_clause", "with_statement", "for_statement", "while_statement",
}  # fmt: skip


class _Extractor:
    def __init__(self, root: Node, src: bytes, lines: list[str], path: str) -> None:
        self.root, self.src, self.lines, self.path = root, src, lines, path
        self.symbols: list[Symbol] = []
        self.node_symbol: dict[int, int] = {}
        self.refs = RefCollector()

    # --- definitions -------------------------------------------------------------------

    def _add(
        self, kind: str, name: str, outer: Node, inner: Node, parent: int | None, prefix: str
    ) -> int:
        start, end = line_range(outer)
        floor = self.symbols[parent].start_line + 1 if parent is not None else 1
        start = attach_leading_comments(self.lines, start, floor, ("#",))
        self.symbols.append(
            Symbol(
                kind=kind,
                name=name,
                qualified_name=f"{self.path}::{prefix}{name}",
                start_line=start,
                end_line=end,
                parent=parent,
                signature=signature_of(self.lines, inner.start_point[0] + 1),
            )
        )
        idx = len(self.symbols) - 1
        self.node_symbol[inner.id] = idx
        return idx

    def _visit(self, container: Node, parent: int | None, prefix: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for child in container.named_children:
            self._definition(child, parent, prefix, depth)

    def _definition(self, node: Node, parent: int | None, prefix: str, depth: int) -> None:
        outer = node
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is None:
                return
            node = inner
        name = node_text(node.child_by_field_name("name"), self.src)
        if node.type == "function_definition" and name:
            in_class = parent is not None and self.symbols[parent].kind == CLASS
            self._add(METHOD if in_class else FUNCTION, name, outer, node, parent, prefix)
        elif node.type == "class_definition" and name:
            idx = self._add(CLASS, name, outer, node, parent, prefix)
            body = node.child_by_field_name("body")
            if body is not None:
                self._visit(body, idx, f"{prefix}{name}.", depth + 1)
        elif node.type in _COMPOUND:
            for child in node.named_children:
                if child.type == "block":
                    self._visit(child, parent, prefix, depth + 1)
                elif child.type.endswith("_clause"):
                    self._definition(child, parent, prefix, depth + 1)

    # --- references --------------------------------------------------------------------

    def _import(self, node: Node, sym: int | None) -> None:
        line = node.start_point[0] + 1
        if node.type == "import_statement":
            for child in node.children_by_field_name("name"):
                if child.type == "aliased_import":
                    module = node_text(child.child_by_field_name("name"), self.src)
                    alias = node_text(child.child_by_field_name("alias"), self.src) or None
                else:
                    module, alias = node_text(child, self.src), None
                self.refs.add_import(module, line, sym, {"names": [], "alias": alias, "level": 0})
            return
        # from [.]module import a, b as c | *
        module_node = node.child_by_field_name("module_name")
        level, module = 0, ""
        if module_node is not None:
            if module_node.type == "relative_import":
                for part in module_node.named_children:
                    if part.type == "import_prefix":
                        level = node_text(part, self.src).count(".")
                    elif part.type == "dotted_name":
                        module = node_text(part, self.src)
            else:
                module = node_text(module_node, self.src)
        names: list[dict[str, str | None]] = []
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                names.append(
                    {
                        "name": node_text(child.child_by_field_name("name"), self.src),
                        "alias": node_text(child.child_by_field_name("alias"), self.src),
                    }
                )
            else:
                names.append({"name": node_text(child, self.src), "alias": None})
        if any(c.type == "wildcard_import" for c in node.named_children):
            names.append({"name": "*", "alias": None})
        self.refs.add_import(
            "." * level + module or ".", line, sym, {"names": names, "level": level}
        )

    def _call(self, node: Node, sym: int | None) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        line = node.start_point[0] + 1
        if fn.type == "identifier":
            self.refs.add_call(node_text(fn, self.src), None, line, sym)
        elif fn.type == "attribute":
            name = node_text(fn.child_by_field_name("attribute"), self.src)
            obj = fn.child_by_field_name("object")
            if obj is not None and obj.type in ("identifier", "attribute"):
                receiver: str | None = node_text(obj, self.src)[:80]
            else:
                receiver = "<expr>"
            self.refs.add_call(name, receiver, line, sym)

    def _collect(self) -> bool:
        """Walk every node once, attributing imports and calls to the innermost symbol."""
        handlers: dict[str, Callable[[Node, int | None], None]] = {
            "import_statement": self._import,
            "import_from_statement": self._import,
            "call": self._call,
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
        self._visit(self.root, None, "", 0)
        truncated = self._collect()
        return ParsedFile(
            symbols=self.symbols,
            refs=self.refs.refs,
            syntax_errors=self.root.has_error,
            truncated=truncated or self.refs.truncated,
        )


def extract(root: Node, src: bytes, lines: list[str], path: str) -> ParsedFile:
    return _Extractor(root, src, lines, path).run()
