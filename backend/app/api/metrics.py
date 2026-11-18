"""Prometheus metrics for a scraper, not for browsers: bearer token, absent unless configured."""

import hmac
from typing import Annotated

from fastapi import Depends, Header, Response

from app.api.deps import AppSettings, ip_limit
from app.core.dbrelease import ReleasingRouter
from app.core.errors import AppError, RepoNotFound
from app.metrics import CONTENT_TYPE, render

router = ReleasingRouter(dependencies=[Depends(ip_limit("metrics", lambda s: 60))])


class BadMetricsToken(AppError):
    status_code = 401
    code = "metrics_unauthorized"


@router.get("/metrics", include_in_schema=False)
def metrics(
    settings: AppSettings, authorization: Annotated[str | None, Header()] = None
) -> Response:
    if settings.metrics_token is None:
        raise RepoNotFound("Not found.")  # the endpoint does not exist unless it is configured
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(
        presented.encode(), settings.metrics_token.get_secret_value().encode()
    ):
        raise BadMetricsToken("Invalid metrics token.")
    return Response(content=render(), media_type=CONTENT_TYPE)
