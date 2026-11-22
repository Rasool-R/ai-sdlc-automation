"""Pydantic schemas for project creation and retrieval endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """Request payload for creating a new engineering project."""

    name: str = Field(min_length=2, max_length=150)
    description: str | None = Field(default=None, max_length=2000)


class ProjectRead(BaseModel):
    """Serialized project record returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """Paginated list of projects."""

    items: list[ProjectRead]
    total: int