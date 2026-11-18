"""A deterministic synthetic Python repository, sized to the app's own caps.

Real-looking enough for the indexer (modules, classes, functions, imports, calls, docstrings with
varied vocabulary) so parsing, chunking, embedding, the graph and search all do real work. It is
not a real codebase: absolute numbers from it are for relative comparison only.
"""

import random

NOUNS = [
    "invoice",
    "ledger",
    "session",
    "token",
    "payment",
    "customer",
    "shipment",
    "inventory",
    "schedule",
    "report",
    "profile",
    "address",
    "catalog",
    "discount",
    "refund",
    "receipt",
    "tenant",
    "webhook",
    "queue",
    "cache",
    "metric",
    "policy",
    "audit",
    "export",
    "import",
    "parser",
    "router",
    "adapter",
    "resolver",
    "validator",
    "scheduler",
    "dispatcher",
]
VERBS = [
    "compute",
    "validate",
    "normalize",
    "serialize",
    "dispatch",
    "resolve",
    "refresh",
    "archive",
    "merge",
    "filter",
    "rank",
    "encode",
    "decode",
    "reconcile",
    "estimate",
    "publish",
    "retry",
]


def generate(n_files: int, seed: int = 7) -> dict[str, str]:
    rng = random.Random(seed)
    files: dict[str, str] = {}
    n_pkgs = max(1, n_files // 12)
    modules = [(f"pkg{i % n_pkgs:02d}", f"mod_{i:04d}") for i in range(n_files)]
    funcs_by_mod: dict[str, list[str]] = {}
    for _, mod in modules:
        funcs_by_mod[mod] = [
            f"{rng.choice(VERBS)}_{rng.choice(NOUNS)}_{rng.randrange(1000)}"
            for _ in range(rng.randint(5, 9))
        ]
    for pkg, mod in modules:
        imports = rng.sample([m for _, m in modules], k=min(3, len(modules)))
        lines = [f'"""{pkg}.{mod}: {rng.choice(NOUNS)} and {rng.choice(NOUNS)} handling."""', ""]
        for other in imports:
            if other != mod:
                other_pkg = next(p for p, m in modules if m == other)
                lines.append(f"from {other_pkg}.{other} import {funcs_by_mod[other][0]}")
        lines += ["", ""]
        cls = f"{rng.choice(NOUNS).title()}{rng.choice(NOUNS).title()}"
        lines += [
            f"class {cls}:",
            f'    """Keeps {rng.choice(NOUNS)} state and applies the {rng.choice(VERBS)} step."""',
            "",
            "    def __init__(self, name, limit=100):",
            "        self.name = name",
            "        self.limit = limit",
            "        self.items = []",
            "",
            f"    def {rng.choice(VERBS)}(self, value):",
            f'        """{rng.choice(VERBS).title()} one {rng.choice(NOUNS)} and remember it."""',
            "        self.items.append(value)",
            "        return len(self.items) <= self.limit",
            "",
            "",
        ]
        for fn in funcs_by_mod[mod]:
            callee_mod = rng.choice(modules)[1]
            callee = funcs_by_mod[callee_mod][0]
            noun, verb = rng.choice(NOUNS), rng.choice(VERBS)
            ret = f"    return {callee}(record)" if callee_mod != mod else "    return total"
            lines += [
                f"def {fn}(record, options=None):",
                f'    """{verb.title()} a {noun} record: checks the {rng.choice(NOUNS)} limit, '
                f'then hands the {rng.choice(NOUNS)} to the next stage."""',
                "    options = options or {}",
                f"    total = sum(len(str(v)) for v in record.values()) + {rng.randrange(100)}",
                f"    if total > options.get('{noun}_limit', {rng.randrange(50, 500)}):",
                f"        raise ValueError('{noun} too large for {verb}')",
                ret,
                "",
                "",
            ]
        files[f"{pkg}/{mod}.py"] = "\n".join(lines)
    files["README.md"] = "# Synthetic repository\n\nGenerated for load testing.\n"
    return files


def queries(n: int = 60, seed: int = 11) -> list[str]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        kind = rng.randrange(3)
        if kind == 0:
            out.append(f"how does the {rng.choice(NOUNS)} {rng.choice(VERBS)} step work")
        elif kind == 1:
            out.append(f"{rng.choice(VERBS)} {rng.choice(NOUNS)}")
        else:
            out.append(f"where is the {rng.choice(NOUNS)} limit checked")
    return out
