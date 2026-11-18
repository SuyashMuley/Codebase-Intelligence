"""Dependency graph and change-impact analysis over an indexed snapshot."""

from typing import Annotated, Any, Literal

from fastapi import Depends, Query

from app.api.deps import (
    DbSession,
    OwnedRepo,
    Providers,
    csrf_guard,
    ip_backstop,
    user_ai_limit,
    user_general_limit,
)
from app.api.files import MAX_PATH_LENGTH, SnapshotParam
from app.core.dbrelease import ReleasingRouter
from app.core.errors import MissingTarget
from app.schemas.graph import AiReviewOut, GraphOut, ImpactOut, ReviewRequest
from app.schemas.repository import ErrorEnvelope
from app.services.graph import queries as graph_queries
from app.services.graph import review
from app.services.indexing import queries

router = ReleasingRouter(
    prefix="/api/repositories/{repository_id}",
    tags=["graph"],
    dependencies=[Depends(ip_backstop), Depends(csrf_guard), Depends(user_general_limit)],
)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in"},
    404: {"model": ErrorEnvelope, "description": "Repository, snapshot, file or symbol not found"},
    409: {"model": ErrorEnvelope, "description": "The graph is not built yet"},
}

PathParam = Annotated[str | None, Query(max_length=MAX_PATH_LENGTH)]


@router.get("/graph", response_model=GraphOut, responses=_ERRORS)
def get_graph(
    repo: OwnedRepo,
    db: DbSession,
    snapshot: SnapshotParam = None,
    level: Literal["file", "symbol"] = "file",
    path: PathParam = None,
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
) -> GraphOut:
    """Files linked by imports (and resolved calls), or, with `level=symbol&path=...`, the
    symbols of one file with their callers and callees. Unresolved calls are labelled as such."""
    snap = queries.resolve_snapshot(db, repo.id, snapshot)
    if level == "symbol":
        if not path:
            raise MissingTarget("The symbol level needs a file path.")
        return graph_queries.symbol_graph(db, snap, path)
    return graph_queries.file_graph(db, snap, path, depth)


@router.get("/impact", response_model=ImpactOut, responses=_ERRORS)
def get_impact(
    repo: OwnedRepo,
    db: DbSession,
    snapshot: SnapshotParam = None,
    file: PathParam = None,
    symbol: Annotated[
        int | None, Query(ge=1, description="A symbol id from the file view.")
    ] = None,
    depth: Annotated[int, Query(ge=1, le=graph_queries.MAX_DEPTH)] = 3,
) -> ImpactOut:
    """What depends on a file or symbol: static dependents with the edge path to each, suggested
    tests (marked when heuristic), and calls that might refer to it but could not be verified."""
    snap = queries.resolve_snapshot(db, repo.id, snapshot)
    if file is None and symbol is None:
        raise MissingTarget("Give a file path or a symbol id.")
    return graph_queries.impact(db, snap, file, symbol, depth)


@router.post(
    "/impact/review",
    response_model=AiReviewOut,
    responses=_ERRORS,
    dependencies=[Depends(user_ai_limit)],
)
def review_impact(
    body: ReviewRequest,
    repo: OwnedRepo,
    providers: Providers,
    db: DbSession,
    snapshot: SnapshotParam = None,
) -> AiReviewOut:
    """An AI reading of the static impact result: risks, tests to consider, open questions.
    Always separate from the static facts, labelled as unverified, and checked so it can only cite
    files the analysis surfaced."""
    llm = providers.need_llm()
    snap = queries.resolve_snapshot(db, repo.id, snapshot)
    if body.file is None and body.symbol is None:
        raise MissingTarget("Give a file path or a symbol id.")
    context = review.prepare(db, snap, body.file, body.symbol, body.depth)
    db.rollback()  # hand the connection back: the model call below can take a minute
    return review.run_review(llm, context)
