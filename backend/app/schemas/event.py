"""Pydantic v2 contracts for persisted workflow progress events and cursor pagination."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowEventRead(BaseModel):
    """Single persisted workflow progress event safe for API and SSE exposure."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(ge=1)
    workflow_id: str
    event_type: str
    status: str
    node_name: str | None = None
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkflowEventListResponse(BaseModel):
    """Cursor-paginated list of persisted workflow events ordered by monotonic ID."""

    workflow_id: str
    items: list[WorkflowEventRead] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    next_cursor: int | None = None
    has_more: bool = False