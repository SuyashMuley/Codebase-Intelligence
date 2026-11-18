"""Shape the question for retrieval. Rules only: a model call per question is too expensive on
a free tier, and the useful signal (does it name a function?) is visible in the text."""

import re
from dataclasses import dataclass

from app.services.retrieval.query import MAX_QUERY_CHARS, query_terms, symbol_names

SHORT_QUESTION_WORDS = 6
MAX_CARRIED_CHARS = 200


@dataclass(frozen=True)
class Classification:
    style: str  # "identifier" (names code), "concept" (describes behaviour), or "mixed"
    names: list[str]  # names in the question that may be functions, methods or classes
    search_query: str  # what to search for


def classify(question: str, previous_user_question: str | None = None) -> Classification:
    names = symbol_names(question)
    terms = query_terms(question)
    if names and len(terms) <= 4:
        style = "identifier"
    elif names:
        style = "mixed"
    else:
        style = "concept"
    query = question.strip()
    # A follow-up like "and what does it return?" means nothing alone: borrow the topic of the
    # question before it.
    if previous_user_question and len(re.findall(r"\w+", query)) < SHORT_QUESTION_WORDS:
        query = f"{previous_user_question.strip()[:MAX_CARRIED_CHARS]} {query}"
    return Classification(style, names, query[:MAX_QUERY_CHARS])
