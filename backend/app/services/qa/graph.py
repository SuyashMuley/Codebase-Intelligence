"""The question-answering workflow, as a LangGraph state machine.

    classify -> scope -> retrieve -+-> expand -> assemble -> generate -> finalize
                                   |                            ^   |
                                   |                      retry +---+  (a citation failed)
                                   +-> cannot_determine         (not enough evidence)

Design rules:
- Without enough evidence there is no model call at all: the honest answer costs nothing.
- One model call per attempt, at most two attempts, all inside one time budget.
- The model's answer is never believed on its own. `validate` checks every claim mechanically
  against the excerpts that were shown; claims that fail are dropped, and the answer says so.
- The database connection is released before the first (slow) model call.
"""

import operator
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, LlmBadResponse, SnapshotNotReady
from app.db.models import Snapshot
from app.providers.embeddings.base import Embedder
from app.services.qa.classify import classify
from app.services.qa.context import (
    SYSTEM,
    build_excerpts,
    build_user_message,
    new_nonce,
)
from app.services.qa.expand import graph_neighbours
from app.services.qa.retrieve import is_strong, is_sufficient, retrieve
from app.services.qa.types import (
    AnswerClaim,
    AnswerResult,
    Citation,
    Excerpt,
    Validation,
)
from app.services.qa.validate import json_object, validate_answer
from app.services.retrieval.search import Hit, _has_embeddings

MIN_SECONDS_FOR_RETRY = 8.0
MAX_ATTEMPTS = 2
LLM_MAX_TOKENS = 1500


class ChatModel(Protocol):
    last_model: str | None

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = ...,
        json_mode: bool = ...,
        temperature: float = ...,
        budget_seconds: float | None = ...,
    ) -> str: ...


@dataclass
class QADeps:
    db: Session
    settings: Settings
    snapshot: Snapshot
    embedder: Embedder | None
    llm: ChatModel
    reserve_quota: Callable[[], None]  # called once, just before the first model call
    refund_quota: Callable[[], None]  # called if the model service fails
    now: Callable[[], float] = time.monotonic


class QAState(TypedDict, total=False):
    question: str
    history: list[tuple[str, str]]
    deadline: float
    trace: Annotated[list[str], operator.add]
    # classify
    style: str
    names: list[str]
    search_query: str
    # scope
    embeddings_ready: bool
    # retrieve
    hits: list[Hit]
    top_similarity: float | None
    symbol_hit: bool
    strong: bool
    retrieval_mode: str
    sufficient: bool
    reason: str
    # expand / assemble
    expanded: list[Hit]
    excerpts: list[Excerpt]
    nonce: str
    # generate / validate
    attempts: int
    feedback: list[str]
    validation: Validation | None
    best: Validation | None
    reserved: bool
    model: str | None
    result: AnswerResult


def build_graph(deps: QADeps) -> Any:
    s, db = deps.settings, deps.db

    def step(name: str, **update: Any) -> dict[str, Any]:
        return {"trace": [name], **update}

    # ---- nodes ------------------------------------------------------------------------------

    def n_classify(state: QAState) -> dict[str, Any]:
        history = state.get("history", [])
        previous = next((c for role, c in reversed(history) if role == "user"), None)
        c = classify(state["question"], previous)
        return step("classify", style=c.style, names=c.names, search_query=c.search_query)

    def n_scope(state: QAState) -> dict[str, Any]:
        if deps.snapshot.status != "ready":
            raise SnapshotNotReady("This repository is still being indexed.")
        ready = deps.embedder is not None and _has_embeddings(db, deps.snapshot.id)
        return step("scope", embeddings_ready=ready)

    def n_retrieve(state: QAState) -> dict[str, Any]:
        r = retrieve(
            db,
            deps.snapshot.id,
            deps.embedder,
            bool(state.get("embeddings_ready")),
            state["search_query"],
            state.get("names", []),
            s.qa_retrieve_k,
            state.get("style") == "overview",
        )
        sufficient = is_sufficient(r, s)
        return step(
            "retrieve",
            hits=r.hits,
            top_similarity=r.top_similarity,
            symbol_hit=r.symbol_hit,
            strong=is_strong(r, s),
            retrieval_mode=r.mode,
            sufficient=sufficient,
            reason="" if sufficient else (r.reason or "no_relevant_code"),
        )

    def n_cannot_determine(state: QAState) -> dict[str, Any]:
        reason = state.get("reason") or "no_relevant_code"
        return step(
            "cannot_determine",
            result=AnswerResult(
                insufficient_evidence=True,
                reason=reason,
                confidence="none",
                missing=_REASON_TEXT.get(reason, _REASON_TEXT["no_relevant_code"]),
                retrieval_mode=state.get("retrieval_mode", "keyword"),
                chunks_considered=len(state.get("hits", [])),
                top_similarity=state.get("top_similarity"),
            ),
        )

    def n_expand(state: QAState) -> dict[str, Any]:
        neighbours = graph_neighbours(db, deps.snapshot.id, state["hits"], s.qa_expand_max)
        return step("expand", expanded=neighbours)

    def n_assemble(state: QAState) -> dict[str, Any]:
        excerpts = build_excerpts(
            state["hits"], state.get("expanded", []), s.qa_context_chunks, s.qa_context_chars
        )
        db.rollback()  # the model call below can take most of a minute: free the connection
        return step("assemble", excerpts=excerpts, nonce=new_nonce(), attempts=0, feedback=[])

    def n_generate(state: QAState) -> dict[str, Any]:
        attempts = state.get("attempts", 0) + 1
        if not state.get("reserved"):
            deps.reserve_quota()
        user = build_user_message(
            state["question"],
            state.get("history", []),
            state["excerpts"],
            state["nonce"],
            state.get("feedback", []),
        )
        remaining = max(0.0, state["deadline"] - deps.now())
        text = ""
        try:
            text = deps.llm.complete(
                SYSTEM,
                user,
                max_tokens=LLM_MAX_TOKENS,
                json_mode=True,
                temperature=0.1,
                budget_seconds=remaining,
            )
        except LlmBadResponse:
            pass  # empty or unreadable: a failed attempt, retried below if there is time
        except AppError:
            deps.refund_quota()
            raise
        try:
            validation = validate_answer(json_object(text), state["excerpts"])
        except LlmBadResponse:
            validation = Validation(issues=["The reply was not a valid JSON object."])
        best = state.get("best")
        if best is None or len(validation.claims) > len(best.claims):
            best = validation
        return step(
            "generate",
            attempts=attempts,
            reserved=True,
            validation=validation,
            best=best,
            model=getattr(deps.llm, "last_model", None),
        )

    def n_finalize(state: QAState) -> dict[str, Any]:
        return step("finalize", result=_finalize(state))

    # ---- edges ------------------------------------------------------------------------------

    def after_retrieve(state: QAState) -> str:
        return "expand" if state.get("sufficient") else "cannot_determine"

    def after_generate(state: QAState) -> str:
        v = state["validation"]
        assert v is not None
        if v.cannot_determine or (v.claims and not v.issues):
            return "finalize"
        time_left = state["deadline"] - deps.now()
        if state["attempts"] < MAX_ATTEMPTS and time_left >= MIN_SECONDS_FOR_RETRY:
            return "retry"
        return "finalize"

    def n_retry(state: QAState) -> dict[str, Any]:
        v = state["validation"]
        assert v is not None
        return step("retry", feedback=v.issues)

    g = StateGraph(QAState)
    g.add_node("classify", n_classify)
    g.add_node("scope", n_scope)
    g.add_node("retrieve", n_retrieve)
    g.add_node("cannot_determine", n_cannot_determine)
    g.add_node("expand", n_expand)
    g.add_node("assemble", n_assemble)
    g.add_node("generate", n_generate)
    g.add_node("retry", n_retry)
    g.add_node("finalize", n_finalize)
    g.add_edge(START, "classify")
    g.add_edge("classify", "scope")
    g.add_edge("scope", "retrieve")
    g.add_conditional_edges(
        "retrieve", after_retrieve, {"expand": "expand", "cannot_determine": "cannot_determine"}
    )
    g.add_edge("cannot_determine", END)
    g.add_edge("expand", "assemble")
    g.add_edge("assemble", "generate")
    g.add_conditional_edges("generate", after_generate, {"retry": "retry", "finalize": "finalize"})
    g.add_edge("retry", "generate")
    g.add_edge("finalize", END)
    return g.compile()


_REASON_TEXT = {
    "no_relevant_code": "No code in this repository was close enough to the question to answer "
    "from. Try naming a function, class or file, or ask about what the code does.",
    "embeddings_not_ready": "Search needs the repository's embeddings, which are not ready yet.",
    "model_declined": "The retrieved code does not contain the answer.",
    "no_supported_claims": "No statement could be tied to a quotation from the retrieved code.",
}


def _finalize(state: QAState) -> AnswerResult:
    excerpts = state["excerpts"]
    final = state.get("validation")
    best = state.get("best")
    assert final is not None and best is not None
    # Verified claims beat a later refusal: they were checked; a refusal is only the model's word.
    chosen = best if best.claims else (final if final.cannot_determine else best)
    base = AnswerResult(
        retrieval_mode=state.get("retrieval_mode", "vector"),
        chunks_considered=len(excerpts),
        expanded=sum(1 for e in excerpts if e.via == "graph"),
        top_similarity=state.get("top_similarity"),
        attempts=state.get("attempts", 1),
        model=state.get("model"),
    )
    if chosen.cannot_determine:
        base.insufficient_evidence = True
        base.reason = "model_declined"
        base.missing = chosen.missing or _REASON_TEXT["model_declined"]
        return base
    if not chosen.claims:
        base.insufficient_evidence = True
        base.reason = "no_supported_claims"
        base.missing = _REASON_TEXT["no_supported_claims"]
        base.dropped_claims = chosen.dropped
        return base

    citations: list[Citation] = []
    index: dict[tuple[int, int, int], int] = {}
    claims: list[AnswerClaim] = []
    for claim in chosen.claims:
        numbers: list[int] = []
        for loc in claim.support:
            key = (loc.chunk_id, loc.start_line, loc.end_line)
            if key not in index:
                index[key] = len(citations) + 1
                citations.append(
                    Citation(index[key], loc.chunk_id, loc.path, loc.start_line, loc.end_line)
                )
            if index[key] not in numbers:
                numbers.append(index[key])
        claims.append(AnswerClaim(claim.text, numbers))
    base.claims, base.citations = claims, citations
    base.dropped_claims = chosen.dropped
    base.missing = chosen.missing
    if chosen.dropped:
        n = chosen.dropped
        base.notes.append(
            f"{n} statement{'s' if n != 1 else ''} the model wrote could not be tied to the code "
            "and "
            f"{'were' if n != 1 else 'was'} removed."
        )
    base.confidence = _confidence(
        dropped=chosen.dropped,
        attempts=base.attempts,
        strong=bool(state.get("strong")),
        claims=len(claims),
    )
    return base


def _confidence(dropped: int, attempts: int, strong: bool, claims: int) -> str:
    """A label with a plain meaning, not a probability.

    high:   strong retrieval, every claim verified, first attempt.
    medium: every claim verified but retrieval moderate or a retry was needed, or some claims
            were removed while two or more verified claims remain with strong retrieval.
    low:    anything else that still has at least one verified claim.
    """
    if dropped == 0 and attempts == 1 and strong:
        return "high"
    if dropped == 0:
        return "medium"
    if strong and claims >= 2:
        return "medium"
    return "low"


def run(
    deps: QADeps,
    question: str,
    history: list[tuple[str, str]],
) -> AnswerResult:
    graph = build_graph(deps)
    final: QAState = graph.invoke(
        {
            "question": question,
            "history": history,
            "deadline": deps.now() + deps.settings.chat_budget_seconds,
            "trace": [],
        }
    )
    result = final["result"]
    result.trace = list(final.get("trace", []))
    return result
