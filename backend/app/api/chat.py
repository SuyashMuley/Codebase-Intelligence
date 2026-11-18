"""Ask a question about an indexed repository and get an answer whose claims are each tied to
verified lines of code, or an honest "cannot determine"."""

import uuid
from typing import Any

from fastapi import Depends

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    OwnedRepo,
    Providers,
    csrf_guard,
    ip_backstop,
    user_chat_limit,
    user_general_limit,
)
from app.core.dbrelease import ReleasingRouter
from app.core.errors import SnapshotNotFound
from app.schemas.chat import (
    ChatAnswer,
    ChatRequest,
    CitationOut,
    ClaimOut,
    RetrievalInfo,
)
from app.schemas.repository import ErrorEnvelope
from app.services.indexing import queries
from app.services.qa import quota
from app.services.qa.graph import QADeps, run

router = ReleasingRouter(
    prefix="/api/repositories/{repository_id}",
    tags=["chat"],
    dependencies=[
        Depends(ip_backstop),
        Depends(csrf_guard),
        Depends(user_general_limit),
        Depends(user_chat_limit),
    ],
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in"},
    404: {"model": ErrorEnvelope, "description": "Repository or snapshot not found"},
    409: {"model": ErrorEnvelope, "description": "The repository is not indexed yet"},
    429: {"model": ErrorEnvelope, "description": "Rate limit or daily question limit"},
    503: {"model": ErrorEnvelope, "description": "The language model is unavailable"},
}


@router.post("/chat", response_model=ChatAnswer, responses=_ERRORS)
def ask(
    body: ChatRequest,
    repo: OwnedRepo,
    providers: Providers,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> ChatAnswer:
    """Answer from the indexed code. Every claim carries a quotation that was checked against
    the cited lines; claims that could not be checked are removed. When nothing relevant is
    found, no model is called and the answer says it cannot determine."""
    llm = providers.need_llm()  # before anything else: a missing provider is the first thing to say
    embedder = providers.embedder()
    if body.snapshot is not None:
        try:
            snapshot_id: uuid.UUID | None = uuid.UUID(body.snapshot)
        except ValueError:
            raise SnapshotNotFound("That snapshot does not exist for this repository.") from None
    else:
        snapshot_id = None
    snapshot = queries.resolve_snapshot(db, repo.id, snapshot_id)

    deps = QADeps(
        db=db,
        settings=settings,
        snapshot=snapshot,
        embedder=embedder,
        llm=llm,
        reserve_quota=lambda: quota.reserve(db, repo.id, user.id, settings.chat_daily_limit),
        refund_quota=lambda: quota.refund(db, repo.id, user.id),
    )
    history: list[tuple[str, str]] = [(t.role, t.content) for t in body.history]
    result = run(deps, body.question, history)
    return ChatAnswer(
        insufficient_evidence=result.insufficient_evidence,
        reason=result.reason,
        confidence=result.confidence,
        claims=[ClaimOut(text=c.text, cites=c.cites) for c in result.claims],
        citations=[
            CitationOut(
                n=c.n,
                chunk_id=c.chunk_id,
                path=c.path,
                start_line=c.start_line,
                end_line=c.end_line,
            )
            for c in result.citations
        ],
        missing=result.missing,
        notes=result.notes,
        dropped_claims=result.dropped_claims,
        model=result.model,
        snapshot={"id": snapshot.id, "commit_sha": snapshot.commit_sha, "state": snapshot.status},
        retrieval=RetrievalInfo(
            mode=result.retrieval_mode,
            chunks_considered=result.chunks_considered,
            expanded_from_graph=result.expanded,
            top_similarity=(
                round(result.top_similarity, 4) if result.top_similarity is not None else None
            ),
        ),
        attempts=result.attempts,
        trace=result.trace,
    )
