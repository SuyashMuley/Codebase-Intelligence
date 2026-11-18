"""Check a model's answer against the excerpts it was given.

The model is an untrusted writer. A claim is kept only if it cites an excerpt that was really
provided AND quotes, verbatim, text that appears in a cited excerpt. The quote also tells us
exactly which lines support the claim, so a citation points at those lines, not at a whole chunk.
Nothing here asks the model whether it is right: it is a mechanical check, so it cannot be
talked out of its decision by text inside the code.

What this does not prove: that the claim is a correct reading of the quoted code. It proves the
claim is anchored to real code, which is the part that can be checked mechanically.
"""

import json
import re

from app.core.errors import LlmBadResponse
from app.services.qa.types import Excerpt, Located, Validation, ValidClaim

MAX_CLAIMS = 6
MAX_CLAIM_CHARS = 400
MIN_QUOTE_CHARS = 8
MAX_QUOTE_CHARS = 300
MAX_MISSING_CHARS = 300


def json_object(text: str) -> dict[str, object]:
    """The JSON object in a model reply, tolerating a code fence or a sentence around it."""
    cleaned = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LlmBadResponse("The language model did not return JSON.")
        cleaned = cleaned[start : end + 1]
    try:
        value = json.loads(cleaned)
    except ValueError:
        raise LlmBadResponse("The language model returned malformed JSON.") from None
    if not isinstance(value, dict):
        raise LlmBadResponse("The language model returned an unexpected shape.")
    return value


def _clean(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _quote_pattern(quote: str) -> re.Pattern[str]:
    """The quote with any run of whitespace matching any run of whitespace (models re-wrap
    lines), and nothing else loosened: case and punctuation must match."""
    words = quote.split()
    return re.compile(r"\s+".join(re.escape(w) for w in words))


def locate(excerpt: Excerpt, quote: str) -> Located | None:
    """The lines of `excerpt` that contain `quote`, or None if it is not there."""
    if not quote.split():
        return None
    match = _quote_pattern(quote).search(excerpt.text)
    if match is None:
        return None
    first = excerpt.start_line + excerpt.text.count("\n", 0, match.start())
    last = excerpt.start_line + excerpt.text.count("\n", 0, match.end())
    return Located(excerpt.alias, excerpt.chunk_id, excerpt.path, first, last)


def validate_answer(draft: dict[str, object], excerpts: list[Excerpt]) -> Validation:
    by_alias = {e.alias: e for e in excerpts}
    out = Validation()
    out.missing = _clean(draft.get("missing"), MAX_MISSING_CHARS)
    if draft.get("cannot_determine") is True:
        out.cannot_determine = True
        return out

    raw_claims = draft.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        out.issues.append("The answer had no claims. Give claims, or set cannot_determine.")
        return out

    seen: set[str] = set()
    for index, item in enumerate(raw_claims[: MAX_CLAIMS * 2], start=1):
        if len(out.claims) >= MAX_CLAIMS:
            out.dropped += 1
            continue
        label = f"Claim {index}"
        if not isinstance(item, dict):
            out.dropped += 1
            out.issues.append(f"{label} was not an object.")
            continue
        text = _clean(item.get("text"), MAX_CLAIM_CHARS)
        if not text:
            out.dropped += 1
            out.issues.append(f"{label} had no text.")
            continue
        if text.lower() in seen:
            continue  # a repeated claim is not an error, just redundant
        seen.add(text.lower())

        cites = item.get("cites")
        cite_list = [c for c in cites if isinstance(c, str)] if isinstance(cites, list) else []
        unknown = [c for c in cite_list if c not in by_alias]
        known = [c for c in dict.fromkeys(cite_list) if c in by_alias]
        if unknown:
            shown = ", ".join(repr(c[:12]) for c in unknown[:3])
            out.issues.append(f"{label} cited {shown}, which is not one of the excerpts.")
        if not known:
            out.dropped += 1
            if not unknown:
                out.issues.append(f"{label} cited no excerpt.")
            continue

        quote = _clean(item.get("quote"), MAX_QUOTE_CHARS + 1)
        if len(quote) < MIN_QUOTE_CHARS or len(quote) > MAX_QUOTE_CHARS:
            out.dropped += 1
            out.issues.append(
                f"{label} needs a verbatim quote of {MIN_QUOTE_CHARS} to {MAX_QUOTE_CHARS} "
                "characters from a cited excerpt."
            )
            continue
        support = [loc for alias in known if (loc := locate(by_alias[alias], quote)) is not None]
        if not support:
            out.dropped += 1
            out.issues.append(
                f"{label}: the quote was not found, word for word, in {', '.join(known)}."
            )
            continue
        out.claims.append(ValidClaim(text=text, support=support))
    return out
