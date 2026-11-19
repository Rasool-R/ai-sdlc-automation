"""Health endpoints providing public liveness and authenticated readiness checks."""

from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.deps import CurrentOperator
from app.core.config import get_settings
from app.db.session import engine
from app.schemas.health import ComponentHealth, LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


def check_postgres() -> ComponentHealth:
    """Verify PostgreSQL connectivity using a lightweight query."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return ComponentHealth(status="ok")
    except (SQLAlchemyError, OSError) as exc:
        return ComponentHealth(status="error", detail=type(exc).__name__)


def check_redis() -> ComponentHealth:
    """Verify Redis connectivity."""
    settings = get_settings()
    try:
        client = Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            is_alive = bool(client.ping())
        finally:
            client.close()
        if is_alive:
            return ComponentHealth(status="ok")
        return ComponentHealth(status="error", detail="RedisPingFailed")
    except (RedisError, OSError, ValueError) as exc:
        return ComponentHealth(status="error", detail=type(exc).__name__)


def check_celery_broker() -> ComponentHealth:
    """Verify Celery broker connectivity."""
    settings = get_settings()
    try:
        client = Redis.from_url(
            settings.CELERY_BROKER_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            is_alive = bool(client.ping())
        finally:
            client.close()
        if is_alive:
            return ComponentHealth(status="ok")
        return ComponentHealth(status="error", detail="BrokerPingFailed")
    except (RedisError, OSError, ValueError) as exc:
        return ComponentHealth(status="error", detail=type(exc).__name__)


@router.get("/live", response_model=LivenessResponse)
def liveness_check() -> LivenessResponse:
    """Public liveness probe exposing no sensitive infrastructure details."""
    settings = get_settings()
    return LivenessResponse(service=settings.APP_NAME, version=__version__)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def readiness_check(_operator: CurrentOperator) -> JSONResponse:
    """Authenticated readiness probe verifying PostgreSQL, Redis, and Celery broker."""
    components = {
        "postgres": check_postgres(),
        "redis": check_redis(),
        "celery_broker": check_celery_broker(),
    }
    all_ok = all(comp.status == "ok" for comp in components.values())
    overall_status: Literal["ready", "degraded"] = "ready" if all_ok else "degraded"
    payload = ReadinessResponse(status=overall_status, components=components)
    http_status = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=http_status, content=payload.model_dump())