from pydantic import BaseModel


class GithubRepoOut(BaseModel):
    owner: str
    name: str
    description: str | None
    language: str | None
    stars: int
    pushed_at: str | None
    fork: bool
    archived: bool


class GithubRepoList(BaseModel):
    items: list[GithubRepoOut]


class GithubPrivateRepoList(GithubRepoList):
    # Where to install the GitHub App on more repositories; null when the app's slug is not
    # configured.
    install_url: str | None = None
    # Days until a private repository's indexed data is deleted automatically; null if never.
    retention_days: int | None = None
