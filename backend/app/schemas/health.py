"""Pydantic schemas for liveness and readiness health check endpoints."""

from typing import Literal

from pydantic import BaseModel


class ComponentHealth(BaseModel):
    """Health check status for an individual infrastructure dependency."""

    status: Literal["ok", "error"]
    detail: str | None = None


class LivenessResponse(BaseModel):
    """Public liveness response indicating the API process is running."""

    status: Literal["alive"] = "alive"
    service: str
    version: str


class ReadinessResponse(BaseModel):
    """Authenticated readiness response covering core infrastructure dependencies."""

    status: Literal["ready", "degraded"]
    components: dict[str, ComponentHealth]