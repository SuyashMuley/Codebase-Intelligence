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
