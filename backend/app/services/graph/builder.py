"""Build the dependency graph of one snapshot from its files, symbols and references.

The rule that shapes everything here: an edge is `static` only when the code itself proves it
(an import that names a file in this repository, a call whose name is bound by such an import or
defined in the same module or package). Anything that would need type information or a guess,
such as `obj.method()` where `obj` is a variable, is stored as `unresolved` with the reason, and
is never used as a fact. The builder never matches by name alone across files.

Known limits, all of which produce unresolved edges rather than wrong ones: inheritance,
type-based method calls, `tsconfig` path aliases, `package.json` workspaces, Go vanity import
paths, dynamic imports and `getattr`-style calls.
"""

import posixpath
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from app.services.graph.model import (
    CALLS,
    IMPORTS,
    STATIC,
    UNRESOLVED,
    Edge,
    FileInfo,
    GraphResult,
    RefInfo,
    SymInfo,
)

_EXPORT_ASSIGN = re.compile(r"^\s*(?:module\.)?exports\.[\w$]+\s*=")
_MEMBER_ASSIGN = re.compile(r"^\s*[\w$]+(?:\.[\w$#]+)+\s*=")

MAX_EDGES = 150_000
MAX_EXPORT_DEPTH = 6
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
# A call target must be something callable or constructible.
CALLABLE_KINDS = frozenset({"function", "class", "struct", "type", "interface", "enum"})
SELF_NAMES = frozenset({"self", "cls", "this"})
CLASS_KINDS = frozenset({"class", "struct", "interface"})


@dataclass(frozen=True)
class ModuleRef:
    """A binding that names a whole file (a module), not a symbol in it."""

    file_id: int


@dataclass(frozen=True)
class PackageRef:
    """A Go package: every non-test file of a directory."""

    dir: str
    file_ids: tuple[int, ...]


@dataclass(frozen=True)
class SymbolBinding:
    """`from x import name`: `name` as exported by file `file_id`."""

    file_id: int
    name: str


Binding = ModuleRef | PackageRef | SymbolBinding
Resolved = SymInfo | ModuleRef | PackageRef


class GraphBuilder:
    def __init__(
        self,
        files: Iterable[FileInfo],
        symbols: Iterable[SymInfo],
        refs: Iterable[RefInfo],
        repo_full_name: str | None = None,
    ) -> None:
        self.files = {f.id: f for f in files}
        self.by_path = {f.path: f for f in self.files.values()}
        self.refs = list(refs)
        self.repo = (repo_full_name or "").lower()
        self.symbols = {s.id: s for s in symbols}

        # `top`: what a file exports under each name. `lexical`: what a bare name means inside the
        # file. They differ for JavaScript property assignments: `exports.f = function` is
        # exported but is not a name in scope, and `obj.f = function` is neither (it is a member
        # of some object, so a bare `f()` cannot refer to it).
        self.top: dict[int, dict[str, list[SymInfo]]] = defaultdict(lambda: defaultdict(list))
        self.lexical: dict[int, dict[str, list[SymInfo]]] = defaultdict(lambda: defaultdict(list))
        self.members: dict[int, dict[str, list[SymInfo]]] = defaultdict(lambda: defaultdict(list))
        for s in self.symbols.values():
            if s.parent_id is None:
                if s.kind != "method":  # Go methods hang off a type we cannot see here
                    sig = s.signature or ""
                    if _EXPORT_ASSIGN.match(sig):
                        self.top[s.file_id][s.name].append(s)
                    elif not _MEMBER_ASSIGN.match(sig):
                        self.top[s.file_id][s.name].append(s)
                        self.lexical[s.file_id][s.name].append(s)
            else:
                self.members[s.parent_id][s.name].append(s)

        self.dir_files: dict[str, list[FileInfo]] = defaultdict(list)
        for f in self.files.values():
            self.dir_files[posixpath.dirname(f.path)].append(f)

        self.package_roots = self._python_roots()
        # Filled while processing imports: per file, local name -> what it refers to.
        self.bindings: dict[int, dict[str, Binding]] = defaultdict(dict)
        self.star_sources: dict[int, list[int]] = defaultdict(list)  # `import *` / `export *`
        self.reexports: dict[int, dict[str, SymbolBinding]] = defaultdict(dict)

        self.edges: list[Edge] = []
        self._seen_import: dict[tuple[int, int], int] = {}
        self._truncated = False

    # ------------------------------------------------------------------ public

    def build(self) -> GraphResult:
        import_refs = [r for r in self.refs if r.kind == "import"]
        call_refs = [r for r in self.refs if r.kind == "call"]
        for ref in import_refs:
            self._process_import(ref)
        static_calls: list[Edge] = []
        unresolved_calls: list[Edge] = []
        merged: dict[tuple[Any, ...], int] = {}
        for ref in call_refs:
            edge = self._process_call(ref)
            bucket = static_calls if edge.resolution == STATIC else unresolved_calls
            key = (
                edge.resolution,
                edge.src_file_id,
                edge.src_symbol_id,
                edge.dst_symbol_id,
                edge.target_name,
            )
            if key in merged:  # same call site resolved twice (e.g. two receivers): add up
                idx = merged[key]
                old = bucket[idx]
                bucket[idx] = replace(old, count=old.count + edge.count)
                continue
            merged[key] = len(bucket)
            bucket.append(edge)
        # Facts first, so the cap can only ever drop unresolved edges.
        ordered = [e for e in self.edges if e.resolution == STATIC] + static_calls
        rest = [e for e in self.edges if e.resolution == UNRESOLVED] + unresolved_calls
        room = max(0, MAX_EDGES - len(ordered))
        truncated = self._truncated or len(ordered) > MAX_EDGES or len(rest) > room
        return GraphResult(edges=(ordered + rest[:room])[:MAX_EDGES], truncated=truncated)

    # ------------------------------------------------------------------ helpers

    def _add_import_edge(self, src: int, dst: int, line: int) -> None:
        key = (src, dst)
        if key in self._seen_import:
            old = self.edges[self._seen_import[key]]
            self.edges[self._seen_import[key]] = replace(old, count=old.count + 1)
            return
        self._seen_import[key] = len(self.edges)
        self.edges.append(Edge(IMPORTS, STATIC, src_file_id=src, dst_file_id=dst, line=line))

    def _unresolved_import(self, ref: RefInfo, reason: str) -> None:
        self.edges.append(
            Edge(
                IMPORTS,
                UNRESOLVED,
                src_file_id=ref.file_id,
                target_name=ref.name[:300],
                reason=reason,
                line=ref.line,
            )
        )

    def _python_roots(self) -> list[str]:
        """Package roots: for each Python file, the first ancestor directory without an
        `__init__.py`. Imports are resolved against these, as the interpreter would."""
        has_init = {f.path for f in self.files.values() if f.path.endswith("/__init__.py")} | {
            f.path for f in self.files.values() if f.path == "__init__.py"
        }
        roots: set[str] = set()
        for f in self.files.values():
            if f.language != "python":
                continue
            d = posixpath.dirname(f.path)
            while d and f"{d}/__init__.py" in has_init:
                d = posixpath.dirname(d)
            roots.add(d)
        return sorted(roots)

    def _python_root_of(self, path: str) -> str:
        d = posixpath.dirname(path)
        while d and f"{d}/__init__.py" in self.by_path:
            d = posixpath.dirname(d)
        return d

    # ------------------------------------------------------------------ imports

    def _process_import(self, ref: RefInfo) -> None:
        lang = self.files[ref.file_id].language
        if lang == "python":
            self._python_import(ref)
        elif lang in ("javascript", "typescript"):
            self._js_import(ref)
        elif lang == "go":
            self._go_import(ref)

    # -- Python

    def _python_module_file(self, importer: FileInfo, module: str, level: int) -> int | None:
        """The file of `module` as imported from `importer`, or None when it is not in this
        repository (standard library, third party) or is ambiguous."""
        parts = [p for p in module.lstrip(".").split(".") if p]
        if level > 0:
            base = posixpath.dirname(importer.path)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            return self._first_python_file(base, parts)
        own = self._python_root_of(importer.path)
        found: list[int] = []
        for root in dict.fromkeys([own, "", *self.package_roots]):
            hit = self._first_python_file(root, parts)
            if hit is not None and hit not in found:
                found.append(hit)
        if not found:
            return None
        if len(found) == 1:
            return found[0]
        own_hit = self._first_python_file(own, parts)
        return own_hit  # the importer's own root wins; otherwise ambiguous -> None

    def _first_python_file(self, root: str, parts: list[str]) -> int | None:
        if not parts:  # `from . import x`: the package itself
            f = self.by_path.get(posixpath.join(root, "__init__.py"))
            return f.id if f is not None else None
        base = posixpath.join(root, *parts)
        for candidate in (f"{base}.py", posixpath.join(base, "__init__.py")):
            f = self.by_path.get(candidate)
            if f is not None:
                return f.id
        return None

    def _python_import(self, ref: RefInfo) -> None:
        importer = self.files[ref.file_id]
        detail = ref.detail
        level = int(detail.get("level", 0))
        module = ref.name.lstrip(".")
        names: list[dict[str, Any]] = detail.get("names") or []

        if not names:  # `import a.b.c [as m]`
            target = self._python_module_file(importer, module, 0)
            if target is None:
                self._unresolved_import(ref, "external_or_unresolved")
                return
            self._add_import_edge(ref.file_id, target, ref.line)
            alias = detail.get("alias")
            self.bindings[ref.file_id][alias or module] = ModuleRef(target)
            return

        target = self._python_module_file(importer, module, level)
        if target is None:
            self._unresolved_import(ref, "external_or_unresolved")
            return
        self._add_import_edge(ref.file_id, target, ref.line)
        for item in names:
            name = item.get("name") or ""
            local = item.get("alias") or name
            if name == "*":
                self.star_sources[ref.file_id].append(target)
                continue
            sub = self._python_module_file(importer, f"{module}.{name}".strip("."), level)
            self.bindings[ref.file_id][local] = SymbolBinding(target, name)
            # `from pkg import sub` may name a submodule rather than something pkg defines. The
            # file is a dependency either way; which one the name means is decided at lookup.
            if sub is not None and sub != target:
                self._add_import_edge(ref.file_id, sub, ref.line)
                self.bindings[ref.file_id].setdefault(f"\0submodule:{local}", ModuleRef(sub))

    # -- JavaScript and TypeScript

    def _js_resolve(self, importer: FileInfo, spec: str) -> int | None:
        if not spec.startswith("."):
            return None
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer.path), spec))
        if base.startswith(".."):
            return None
        if base == ".":  # `../..` and friends: the repository root directory
            base = ""
        candidates = [base]
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):  # TypeScript ESM writes .js for .ts files
            candidates += [stem + ".ts", stem + ".tsx"]
        candidates += [base + e for e in JS_EXTENSIONS]
        candidates += [posixpath.join(base, "index" + e) for e in JS_EXTENSIONS]
        for c in candidates:
            f = self.by_path.get(c)
            if f is not None and f.language in ("javascript", "typescript"):
                return f.id
        return None

    def _js_import(self, ref: RefInfo) -> None:
        importer = self.files[ref.file_id]
        detail = ref.detail
        kind = detail.get("kind")
        target = self._js_resolve(importer, ref.name)
        if target is None:
            reason = "dynamic_import" if kind == "dynamic" else "package_or_alias"
            self._unresolved_import(ref, reason)
            return
        self._add_import_edge(ref.file_id, target, ref.line)
        b = self.bindings[ref.file_id]
        if kind == "reexport":
            for item in detail.get("names") or []:
                exported = item.get("alias") or item.get("name")
                if exported and item.get("name"):
                    self.reexports[ref.file_id][exported] = SymbolBinding(target, item["name"])
            if detail.get("star"):
                self.star_sources[ref.file_id].append(target)
            return
        if kind == "require":
            binding = detail.get("binding") or ""
            if binding.startswith("{"):
                for local, orig in _object_pattern(binding):
                    b[local] = SymbolBinding(target, orig)
            elif binding.isidentifier():
                b[binding] = ModuleRef(target)
            return
        if kind != "import":
            return
        if detail.get("default"):
            b[detail["default"]] = SymbolBinding(target, "default")
        if detail.get("namespace"):
            b[detail["namespace"]] = ModuleRef(target)
        for item in detail.get("names") or []:
            name = item.get("name")
            if name:
                b[item.get("alias") or name] = SymbolBinding(target, name)

    # -- Go

    def _go_import(self, ref: RefInfo) -> None:
        detail = ref.detail
        path = ref.name
        prefix = f"github.com/{self.repo}"
        lowered = path.lower()
        if not self.repo or not (lowered == prefix or lowered.startswith(prefix + "/")):
            self._unresolved_import(ref, "external_package")
            return
        directory = path[len(prefix) :].strip("/")
        files = [f for f in self.dir_files.get(directory, []) if _is_go_source(f)]
        if not files:
            self._unresolved_import(ref, "package_not_found")
            return
        for f in files:
            self._add_import_edge(ref.file_id, f.id, ref.line)
        if detail.get("blank"):
            return
        if detail.get("dot"):
            self.star_sources[ref.file_id].extend(f.id for f in files)
            return
        name = detail.get("alias") or _go_package_name(files) or posixpath.basename(directory)
        self.bindings[ref.file_id][name] = PackageRef(directory, tuple(f.id for f in files))

    # ------------------------------------------------------------------ exports

    def _unique(self, symbols: list[SymInfo]) -> SymInfo | None:
        callable_ = [s for s in symbols if s.kind in CALLABLE_KINDS]
        return callable_[0] if len(callable_) == 1 else None

    def resolve_export(
        self,
        file_id: int,
        name: str,
        depth: int = 0,
        seen: frozenset[tuple[int, str]] = frozenset(),
    ) -> Resolved | None:
        """What `name` means when imported from `file_id`: a symbol it defines, or whatever it
        re-exports under that name, followed up to a few hops."""
        if depth > MAX_EXPORT_DEPTH or (file_id, name) in seen:
            return None
        seen = seen | {(file_id, name)}
        defined = self.top.get(file_id, {}).get(name)
        if defined:
            return self._unique(defined)  # several definitions of one name: ambiguous
        re = self.reexports.get(file_id, {}).get(name)
        if re is not None:
            return self.resolve_export(re.file_id, re.name, depth + 1, seen)
        bound = self.bindings.get(file_id, {}).get(name)
        if isinstance(bound, SymbolBinding):
            hit = self.resolve_export(bound.file_id, bound.name, depth + 1, seen)
            if hit is not None:
                return hit
        elif isinstance(bound, ModuleRef | PackageRef):
            return bound
        sub = self.bindings.get(file_id, {}).get(f"\0submodule:{name}")
        if isinstance(sub, ModuleRef):
            return sub
        found = {
            r
            for src in self.star_sources.get(file_id, [])
            if (r := self.resolve_export(src, name, depth + 1, seen)) is not None
        }
        return next(iter(found)) if len(found) == 1 else None

    def _resolve_binding(self, file_id: int, local: str, b: Binding) -> Resolved | None:
        if not isinstance(b, SymbolBinding):
            return b
        hit = self.resolve_export(b.file_id, b.name)
        if hit is not None:
            return hit
        # `from pkg import sub`: the name was a submodule, not something pkg defines.
        sub = self.bindings.get(file_id, {}).get(f"\0submodule:{local}")
        return sub if isinstance(sub, ModuleRef) else None

    # ------------------------------------------------------------------ calls

    def _enclosing_class(self, symbol_id: int | None) -> SymInfo | None:
        cur = self.symbols.get(symbol_id) if symbol_id is not None else None
        while cur is not None:
            if cur.kind in CLASS_KINDS:
                return cur
            cur = self.symbols.get(cur.parent_id) if cur.parent_id is not None else None
        return None

    def _member(self, base: Resolved | None, name: str) -> Resolved | None:
        if isinstance(base, SymInfo):
            if base.kind not in CLASS_KINDS:
                return None
            return self._unique_member(base.id, name)
        if isinstance(base, ModuleRef):
            return self.resolve_export(base.file_id, name)
        if isinstance(base, PackageRef):
            hits = [
                s
                for fid in base.file_ids
                for s in self.top.get(fid, {}).get(name, [])
                if s.kind in CALLABLE_KINDS
            ]
            return hits[0] if len(hits) == 1 else None
        return None

    def _unique_member(self, class_id: int, name: str) -> SymInfo | None:
        hits = self.members.get(class_id, {}).get(name, [])
        return hits[0] if len(hits) == 1 else None

    def _lookup_name(self, file: FileInfo, name: str) -> Resolved | None:
        bound = self.bindings.get(file.id, {}).get(name)
        local = self.lexical.get(file.id, {}).get(name)
        if bound is not None and local:
            return None  # defined and imported under one name: ambiguous
        if bound is not None:
            return self._resolve_binding(file.id, name, bound)
        if local:
            return self._unique(local)
        if file.language == "go":
            return self._go_package_scope(file, name)
        found = {
            r
            for src in self.star_sources.get(file.id, [])
            if (r := self.resolve_export(src, name)) is not None
        }
        return next(iter(found)) if len(found) == 1 else None

    def _go_package_scope(self, file: FileInfo, name: str) -> SymInfo | None:
        """Plain calls in Go may name anything declared in the same package (same directory)."""
        siblings = [
            f
            for f in self.dir_files.get(posixpath.dirname(file.path), [])
            if f.language == "go" and f.go_package == file.go_package
        ]
        hits = [
            s
            for f in siblings
            for s in self.top.get(f.id, {}).get(name, [])
            if s.kind in CALLABLE_KINDS
        ]
        if hits:
            return hits[0] if len(hits) == 1 else None
        # dot-imported packages bring their names into scope
        found = [
            s
            for src in self.star_sources.get(file.id, [])
            for s in self.top.get(src, {}).get(name, [])
            if s.kind in CALLABLE_KINDS
        ]
        return found[0] if len(found) == 1 else None

    def _process_call(self, ref: RefInfo) -> Edge:
        file = self.files[ref.file_id]
        receiver = ref.detail.get("receiver")
        count = int(ref.detail.get("count", 1))
        shown = f"{receiver}.{ref.name}" if receiver else ref.name
        target, reason = self._resolve_call(file, ref, receiver)
        if isinstance(target, SymInfo):
            return Edge(
                CALLS,
                STATIC,
                src_file_id=ref.file_id,
                dst_file_id=target.file_id,
                src_symbol_id=ref.symbol_id,
                dst_symbol_id=target.id,
                target_name=shown[:300],
                line=ref.line,
                count=count,
            )
        return Edge(
            CALLS,
            UNRESOLVED,
            src_file_id=ref.file_id,
            src_symbol_id=ref.symbol_id,
            target_name=shown[:300],
            reason=reason,
            line=ref.line,
            count=count,
        )

    def _resolve_call(
        self, file: FileInfo, ref: RefInfo, receiver: str | None
    ) -> tuple[Resolved | None, str]:
        name = ref.name
        if receiver is None:
            target = self._lookup_name(file, name)
            return (target, "") if isinstance(target, SymInfo) else (None, "not_found")
        if receiver == "<expr>":
            return None, "dynamic_receiver"
        if receiver == "super":
            return None, "inherited_or_dynamic"
        if receiver in SELF_NAMES and file.language != "go":
            cls = self._enclosing_class(ref.symbol_id)
            if cls is None:
                return None, "dynamic_receiver"
            hit = self._unique_member(cls.id, name)
            return (hit, "") if hit else (None, "inherited_or_dynamic")
        if file.language == "go":
            # Only `pkg.Func(...)` is resolvable; anything else needs the receiver's type.
            if "." in receiver:
                return None, "dynamic_receiver"
            bound = self.bindings.get(file.id, {}).get(receiver)
            if isinstance(bound, PackageRef):
                member = self._member(bound, name)
                return (member, "") if isinstance(member, SymInfo) else (None, "not_found")
            return None, "dynamic_receiver"
        base = self._resolve_receiver(file, receiver)
        if base is None:
            return None, "dynamic_receiver"
        member = self._member(base, name)
        return (member, "") if isinstance(member, SymInfo) else (None, "not_found")

    def _resolve_receiver(self, file: FileInfo, receiver: str) -> Resolved | None:
        """`a.b.c` as a chain of module, package or class names, or None if any link is a
        variable (whose type we do not know)."""
        direct = self.bindings.get(file.id, {}).get(receiver)  # `import a.b.c` binds the whole
        if isinstance(direct, ModuleRef | PackageRef):
            return direct
        parts = receiver.split(".")
        cur = self._lookup_name(file, parts[0])
        for part in parts[1:]:
            if cur is None:
                return None
            cur = self._member(cur, part)
        return cur


def _is_go_source(f: FileInfo) -> bool:
    return f.language == "go" and not f.path.endswith("_test.go")


def _go_package_name(files: list[FileInfo]) -> str | None:
    names = {f.go_package for f in files if f.go_package}
    return next(iter(names)) if len(names) == 1 else None


def _object_pattern(binding: str) -> list[tuple[str, str]]:
    """`{ a, b: c }` -> [("a", "a"), ("c", "b")]. Anything fancier (defaults, rest, nesting) is
    skipped rather than guessed."""
    inner = binding.strip()[1:-1] if binding.rstrip().endswith("}") else ""
    out: list[tuple[str, str]] = []
    for part in inner.split(","):
        part = part.strip()
        if not part or any(ch in part for ch in "{[=."):
            continue
        orig, _, local = part.partition(":")
        orig, local = orig.strip(), (local.strip() or orig.strip())
        if orig.isidentifier() and local.isidentifier():
            out.append((local, orig))
    return out
