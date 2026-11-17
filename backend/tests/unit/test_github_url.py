import pytest

from app.core.errors import InvalidGithubUrl
from app.services.github.url import RepoRef, parse_github_url


@pytest.mark.parametrize(
    ("raw", "owner", "name"),
    [
        ("https://github.com/pallets/flask", "pallets", "flask"),
        ("https://github.com/pallets/flask/", "pallets", "flask"),
        ("https://github.com/pallets/flask.git", "pallets", "flask"),
        ("https://www.github.com/pallets/flask", "pallets", "flask"),
        ("  https://github.com/pallets/flask  ", "pallets", "flask"),
        ("https://GitHub.com/Pallets/Flask", "Pallets", "Flask"),
        ("https://github.com/a/b", "a", "b"),
        ("https://github.com/my-org/my.repo_name-2", "my-org", "my.repo_name-2"),
        ("https://github.com/" + "a" * 39 + "/x", "a" * 39, "x"),
    ],
)
def test_accepts_valid_urls(raw: str, owner: str, name: str) -> None:
    ref = parse_github_url(raw)
    assert ref == RepoRef(owner=owner, name=name)
    assert ref.url == f"https://github.com/{owner}/{name}"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "github.com/pallets/flask",  # no scheme
        "http://github.com/pallets/flask",  # not https
        "ftp://github.com/pallets/flask",
        "https://gitlab.com/pallets/flask",  # wrong host
        "https://github.com.evil.com/pallets/flask",  # look-alike host
        "https://evil.com/github.com/pallets/flask",
        "https://github.com@evil.com/pallets/flask",  # userinfo trick
        "https://user:pw@github.com/pallets/flask",  # credentials
        "https://github.com:8443/pallets/flask",  # custom port
        "https://github.com/pallets",  # missing repo
        "https://github.com/",
        "https://github.com/pallets/flask/tree/main",  # extra path
        "https://github.com/pallets/flask?tab=readme",  # query
        "https://github.com/pallets/flask#readme",  # fragment
        "https://github.com/pallets/../etc",  # traversal
        "https://github.com/pallets/..",
        "https://github.com/pallets/.",
        "https://github.com//flask",  # empty owner
        "https://github.com/pallets/%2e%2e",  # percent-encoded
        "https://github.com/-bad/flask",  # owner starts with hyphen
        "https://github.com/bad-/flask",  # owner ends with hyphen
        "https://github.com/" + "a" * 40 + "/x",  # owner too long
        "https://github.com/pallets/" + "x" * 101,  # repo too long
        "https://github.com/pallets/fl ask",  # whitespace
        "https://github.com/pallets/fl\nask",  # control char inside
        "https://github.com\\pallets\\flask",  # backslashes
        "https://github.com/pallets/fla$k",  # illegal char
        "https://" + "github.com/" + "a/" * 120,  # over length limit
    ],
)
def test_rejects_invalid_urls(raw: str) -> None:
    with pytest.raises(InvalidGithubUrl) as exc:
        parse_github_url(raw)
    assert exc.value.code == "invalid_github_url"
    assert exc.value.status_code == 400
    assert "reason" in exc.value.details
