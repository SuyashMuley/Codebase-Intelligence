from typing import Annotated, Any

from fastapi import Depends, Response, status

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    OwnedRepo,
    Providers,
    csrf_guard,
    get_github,
    ip_backstop,
    user_general_limit,
    user_index_limit,
)
from app.core.dbrelease import ReleasingRouter
from app.schemas.indexing import IndexRequest, IndexStartOut, StatusOut, TickOut
from app.schemas.repository import ErrorEnvelope
from app.services.github.client import GitHubGateway
from app.services.indexing import runner, service  # noqa: F401  (import registers phase handlers)
from app.services.repos import retention

router = ReleasingRouter(
    prefix="/api/repositories/{repository_id}",
    tags=["indexing"],
    dependencies=[Depends(ip_backstop), Depends(csrf_guard), Depends(user_general_limit)],
)

Github = Annotated[GitHubGateway, Depends(get_github)]

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in, or GitHub authorization expired"},
    404: {"model": ErrorEnvelope, "description": "Repository or ref not found"},
    409: {"model": ErrorEnvelope, "description": "Conflicting indexing job"},
    429: {"model": ErrorEnvelope, "description": "Rate limit or quota exceeded"},
    503: {"model": ErrorEnvelope, "description": "GitHub rate limited or unavailable"},
}


@router.post(
    "/index",
    response_model=IndexStartOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(user_index_limit)],
    responses={200: {"model": IndexStartOut, "description": "Commit already known"}, **_ERRORS},
)
def start_index(
    repo: OwnedRepo,
    providers: Providers,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    gh: Github,
    user: CurrentUser,
    body: IndexRequest | None = None,
) -> IndexStartOut:
    """Pin a commit and queue it for indexing. Drive it forward with `POST .../index/tick`.

    Indexing the same commit twice returns the existing snapshot instead of repeating work;
    a failed one is restarted. Send `{"force": true}` to index the commit again from scratch.
    """
    if repo.is_private:
        providers.need_both()  # policy gate: both slots must be the owner's own
    snapshot, job, created = service.start_indexing(
        db, repo, body.ref if body else None, user.id, gh, settings, force=bool(body and body.force)
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return IndexStartOut(
        created=created,
        status=service.build_status(
            snapshot, job, settings.max_attempts, retention.retention_days(repo, settings)
        ),
    )


@router.post("/index/tick", response_model=TickOut, responses=_ERRORS)
def tick_index(
    repo: OwnedRepo, providers: Providers, db: DbSession, settings: AppSettings, gh: Github
) -> TickOut:
    """Run one bounded step of the active indexing job. Safe to call repeatedly or concurrently."""
    # A private repository is embedded with its owner's provider only; a public one uses the
    # platform's (the runner's default).
    embedder = providers.need_embedder() if repo.is_private else None
    job = service.active_job(db, repo.id)
    result = runner.tick(db, job.id, gh, settings, embedder)
    return TickOut(
        ran=result.ran,
        reason=result.reason,
        status=service.latest_status(
            db, repo.id, settings.max_attempts, retention.retention_days(repo, settings)
        ),
    )


@router.get("/status", response_model=StatusOut, responses=_ERRORS)
def get_status(repo: OwnedRepo, db: DbSession, settings: AppSettings) -> StatusOut:
    return service.latest_status(
        db, repo.id, settings.max_attempts, retention.retention_days(repo, settings)
    )
