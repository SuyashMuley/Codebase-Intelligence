"""Browse the signed-in user's own public GitHub repositories."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_github, ip_backstop, user_general_limit
from app.schemas.github import GithubRepoList, GithubRepoOut
from app.schemas.repository import ErrorEnvelope
from app.services.github.client import GitHubGateway

router = APIRouter(
    prefix="/api/github",
    tags=["github"],
    dependencies=[Depends(ip_backstop), Depends(user_general_limit)],
)

Github = Annotated[GitHubGateway, Depends(get_github)]

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in, or GitHub authorization expired"},
    503: {"model": ErrorEnvelope, "description": "GitHub rate limited or unavailable"},
}


@router.get("/repos", response_model=GithubRepoList, responses=_ERRORS)
def my_public_repos(user: CurrentUser, gh: Github) -> GithubRepoList:
    """Your public repositories on GitHub (the ones you own), most recently pushed first.

    These are suggestions for the connect step; nothing is stored until you connect one.
    """
    repos = gh.list_public_repos(user.login)
    return GithubRepoList(
        items=[
            GithubRepoOut(
                owner=r.owner,
                name=r.name,
                description=r.description,
                language=r.language,
                stars=r.stars,
                pushed_at=r.pushed_at,
                fork=r.fork,
                archived=r.archived,
            )
            for r in repos
        ]
    )
