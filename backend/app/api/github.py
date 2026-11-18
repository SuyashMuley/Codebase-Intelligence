"""Browse the signed-in user's own public GitHub repositories."""

from typing import Annotated, Any

from fastapi import Depends

from app.api.deps import AppSettings, CurrentUser, get_github, ip_backstop, user_general_limit
from app.core.dbrelease import ReleasingRouter
from app.schemas.github import GithubPrivateRepoList, GithubRepoList, GithubRepoOut
from app.schemas.repository import ErrorEnvelope
from app.services.github.client import GitHubGateway, GithubRepoInfo

router = ReleasingRouter(
    prefix="/api/github",
    tags=["github"],
    dependencies=[Depends(ip_backstop), Depends(user_general_limit)],
)

Github = Annotated[GitHubGateway, Depends(get_github)]

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Not signed in, or GitHub authorization expired"},
    503: {"model": ErrorEnvelope, "description": "GitHub rate limited or unavailable"},
}


def _out(r: GithubRepoInfo) -> GithubRepoOut:
    return GithubRepoOut(
        owner=r.owner,
        name=r.name,
        description=r.description,
        language=r.language,
        stars=r.stars,
        pushed_at=r.pushed_at,
        fork=r.fork,
        archived=r.archived,
    )


@router.get("/repos", response_model=GithubRepoList, responses=_ERRORS)
def my_public_repos(user: CurrentUser, gh: Github) -> GithubRepoList:
    """Your public repositories on GitHub (the ones you own), most recently pushed first.

    These are suggestions for the connect step; nothing is stored until you connect one.
    """
    repos = gh.list_public_repos(user.login)
    return GithubRepoList(items=[_out(r) for r in repos])


@router.get("/private-repos", response_model=GithubPrivateRepoList, responses=_ERRORS)
def my_private_repos(user: CurrentUser, gh: Github, settings: AppSettings) -> GithubPrivateRepoList:
    """Private repositories you can connect: the ones you can see on GitHub that the GitHub App
    is installed on. Connecting one needs your own embedding and chat providers.

    `install_url` is where to install the app on more repositories, when it is configured."""
    slug = settings.github_app_slug
    return GithubPrivateRepoList(
        items=[_out(r) for r in gh.list_private_repos()],
        install_url=f"https://github.com/apps/{slug}/installations/new" if slug else None,
        retention_days=settings.private_retention_days or None,
    )
