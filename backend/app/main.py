"""FastAPI application factory, lifecycle management, middleware, and error handlers."""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import __version__
from app.api.v1.router import api_v1_router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, correlation_id_ctx

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Validate settings and configure secret-redacting logging on startup."""
    settings = get_settings()
    extra_secrets = [
        settings.APP_SECRET_KEY,
        settings.OPERATOR_PASSWORD_HASH,
        settings.CREDENTIAL_ENCRYPTION_KEYS,
        settings.LLM_API_KEY,
        settings.EMBEDDING_API_KEY,
        settings.LANGSMITH_API_KEY,
        settings.GITHUB_PAT_FALLBACK_TOKEN,
        settings.GITHUB_WEBHOOK_SECRET,
        settings.QDRANT_API_KEY,
    ]
    configure_logging(
        level=settings.LOG_LEVEL,
        log_format=settings.LOG_FORMAT,
        extra_secrets=extra_secrets,
    )
    logger.info(
        "Application startup complete",
        extra={"structured_data": {"env": settings.APP_ENV, "version": __version__}},
    )
    yield
    logger.info("Application shutdown initiated")


async def handle_app_error(_request: Request, exc: Exception) -> JSONResponse:
    """Convert structured AppError instances into consistent JSON error responses."""
    if isinstance(exc, AppError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
                "details": {},
            }
        },
    )


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    application = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def correlation_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming_id = request.headers.get("X-Correlation-ID", "").strip()
        corr_id = incoming_id if incoming_id and len(incoming_id) <= 128 else str(uuid.uuid4())
        token = correlation_id_ctx.set(corr_id)
        try:
            response = await call_next(request)
        finally:
            correlation_id_ctx.reset(token)
        response.headers["X-Correlation-ID"] = corr_id
        return response

    application.add_exception_handler(AppError, handle_app_error)
    application.include_router(api_v1_router, prefix="/api/v1")
    return application


app = create_app()