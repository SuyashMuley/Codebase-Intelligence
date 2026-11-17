import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.errors import error_response
from app.core.logging import request_id_var

logger = logging.getLogger("app.request")

# Only accept a client-supplied ID if it is harmless to put in logs and headers.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID, logs one structured line per request, and turns
    unexpected exceptions into the standard error envelope."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _SAFE_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("unhandled_error", extra={"path": request.url.path})
                response = error_response(500, "internal_error", "Internal server error.")
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            return response
        finally:
            request_id_var.reset(token)
