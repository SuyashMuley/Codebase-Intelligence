import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class AppError(Exception):
    """An expected, client-visible error. Rendered as the standard error envelope."""

    status_code = 400
    code = "bad_request"
    # Retryable errors are transient: the indexing job backs off and tries again.
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        self.headers = headers or {}


class InvalidGithubUrl(AppError):
    status_code = 400
    code = "invalid_github_url"


class RepoNotFound(AppError):
    status_code = 404
    code = "repo_not_found"


class RepoAccessLost(AppError):
    """GitHub no longer shows this private repository to its owner."""

    status_code = 403
    code = "repo_access_lost"


class ProvidersRequired(AppError):
    """A private repository may only use providers its owner supplied."""

    status_code = 409
    code = "providers_required"


class InvalidProviderConfig(AppError):
    status_code = 422
    code = "invalid_provider"


class ProviderAddressRefused(AppError):
    """A provider address failed the SSRF guard (not https, not allowlisted, or internal)."""

    status_code = 502
    code = "provider_address_refused"


class NotReady(AppError):
    status_code = 503
    code = "not_ready"


class TooManyRequests(AppError):
    """This API's own per-client rate limit."""

    status_code = 429
    code = "rate_limited"


class QuotaExceeded(AppError):
    status_code = 429
    code = "quota_exceeded"


class RepoTooLarge(AppError):
    status_code = 422
    code = "repo_too_large"


class JobInProgress(AppError):
    status_code = 409
    code = "job_in_progress"


class NoActiveJob(AppError):
    status_code = 409
    code = "no_active_job"


class SnapshotNotReady(AppError):
    status_code = 409
    code = "snapshot_not_ready"


class ChatQuotaExceeded(AppError):
    status_code = 429
    code = "chat_quota_exceeded"


class MissingTarget(AppError):
    status_code = 422
    code = "missing_target"


class GraphNotReady(AppError):
    status_code = 409
    code = "graph_not_ready"


class SymbolNotFound(AppError):
    status_code = 404
    code = "symbol_not_found"


class SnapshotNotFound(AppError):
    status_code = 404
    code = "snapshot_not_found"


class FileNotFound(AppError):
    status_code = 404
    code = "file_not_found"


class EmbeddingNotConfigured(AppError):
    status_code = 503
    code = "embedding_not_configured"


class EmbeddingAuthFailed(AppError):
    status_code = 502
    code = "embedding_auth_failed"


class EmbeddingQuotaExceeded(AppError):
    """The embedding provider's allowance is used up. Not retried: try again later."""

    status_code = 503
    code = "embedding_quota_exceeded"


class EmbeddingUnavailable(AppError):
    status_code = 502
    code = "embedding_unavailable"
    retryable = True


class LlmNotConfigured(AppError):
    status_code = 503
    code = "llm_not_configured"


class LlmAuthFailed(AppError):
    status_code = 502
    code = "llm_auth_failed"


class LlmRateLimited(AppError):
    status_code = 503
    code = "llm_rate_limited"
    retryable = True


class LlmUnavailable(AppError):
    status_code = 502
    code = "llm_unavailable"
    retryable = True


class LlmRequestTooLarge(AppError):
    """The question and the code it needs are bigger than any available model accepts."""

    status_code = 413
    code = "llm_request_too_large"


class LlmBadResponse(AppError):
    status_code = 502
    code = "llm_bad_response"


class EmbeddingBadResponse(AppError):
    status_code = 502
    code = "embedding_bad_response"


class GithubRateLimited(AppError):
    """GitHub's rate limit (upstream), not ours. Retried after the reset time."""

    status_code = 503
    code = "github_rate_limited"
    retryable = True


class GithubUnavailable(AppError):
    status_code = 502
    code = "github_unavailable"
    retryable = True


class Unauthenticated(AppError):
    status_code = 401
    code = "unauthenticated"


class ReauthRequired(AppError):
    """The user's GitHub authorization is gone or was revoked; they must sign in again."""

    status_code = 401
    code = "reauth_required"


class ForbiddenOrigin(AppError):
    status_code = 403
    code = "forbidden_origin"


class AuthNotConfigured(AppError):
    status_code = 503
    code = "auth_not_configured"


class GithubAuthFailed(AppError):
    status_code = 502
    code = "github_auth_failed"


class InternalError(AppError):
    """Unexpected failure inside a job step. Retried (bounded), since many are transient."""

    status_code = 500
    code = "internal_error"
    retryable = True


class FetchTimeout(AppError):
    status_code = 504
    code = "fetch_timeout"
    retryable = True


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id_var.get(),
        }
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # `input` and `ctx` can echo user data or hold non-serializable objects; keep it minimal.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return error_response(
            422,
            "validation_error",
            "Request validation failed.",
            {"errors": jsonable_encoder(errors)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return error_response(exc.status_code, code, str(exc.detail))
