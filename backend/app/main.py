from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, files, health, indexing, repositories, search
from app.api import github as github_api
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import SlidingWindowLimiter


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Codebase Intelligence Platform API",
        version="0.1.0",
        description="Ingest public GitHub repositories, explore them, and ask questions.",
    )

    app.state.limiter = SlidingWindowLimiter()

    # Added last = outermost, so the request ID and error envelope also cover CORS responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(github_api.router)
    app.include_router(repositories.router)
    app.include_router(indexing.router)
    app.include_router(files.router)
    app.include_router(search.router)
    return app


app = create_app()
