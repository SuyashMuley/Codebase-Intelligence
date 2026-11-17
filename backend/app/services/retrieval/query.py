"""Turning a natural-language question into keyword search terms."""

import re

_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Question words and glue that appear in almost every question and carry no code meaning.
STOPWORDS = frozenset(
    """a an the is are was were be been being am do does did done doing have has had having
    i me my we our you your it its this that these those there here
    of to in on at by for from with without into onto over under about as than then so
    and or but if not no nor can could should would will shall may might must
    how what where when why who whom whose which whether
    use used uses using please show tell find get gets give""".split()
)

MAX_TERMS = 24
MAX_QUERY_CHARS = 500
MAX_NAMES = 8

_BACKTICKED = re.compile(r"`([^`\n]{1,120})`")
_DOTTED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def query_terms(text: str) -> list[str]:
    """Lowercase alphanumeric search terms, stopwords removed. A camelCase identifier yields both
    the whole word and its parts, matching how chunks are indexed. Output is safe to splice into
    a tsquery: letters and digits only."""
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.lower()
        if term and term not in seen and term not in STOPWORDS:
            seen.add(term)
            terms.append(term)

    for word in _WORD.findall(text[:MAX_QUERY_CHARS]):
        add(word)
        parts = _CAMEL.split(word)
        if len(parts) > 1:
            for part in parts:
                add(part)
    return terms[:MAX_TERMS]


def to_tsquery(terms: list[str]) -> str:
    """'a | b | c': a chunk matches if it contains any term; ranking decides the order."""
    return " | ".join(terms)


def _looks_like_a_name(segment: str, quoted: bool) -> bool:
    """snake_case, camelCase/PascalCase, or anything the user put in backticks."""
    if len(segment) < 3 or segment.lower() in STOPWORDS:
        return False
    return quoted or "_" in segment.strip("_") or bool(re.search(r"[a-z0-9][A-Z]", segment))


def symbol_names(text: str) -> list[str]:
    """Names in the question that probably refer to a function, method or class.

    `HTTPDigestAuth.build_digest_header` yields both parts. Plain lowercase words are only
    taken when quoted in backticks, so ordinary English is never mistaken for a symbol.
    Lowercased, de-duplicated and capped; letters, digits and underscores only.
    """
    text = text[:MAX_QUERY_CHARS]
    spans = [(m.group(1), True) for m in _BACKTICKED.finditer(text)]
    spans.append((_BACKTICKED.sub(" ", text), False))
    names: list[str] = []
    for chunk, quoted in spans:
        for dotted in _DOTTED_NAME.findall(chunk):
            for segment in dotted.split("."):
                lowered = segment.lower()
                if lowered not in names and _looks_like_a_name(segment, quoted):
                    names.append(lowered)
    return names[:MAX_NAMES]
