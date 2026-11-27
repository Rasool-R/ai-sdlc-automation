"""Authenticated REST and Server-Sent Events (SSE) endpoint for workflow progress."""

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentOperator, DbSession
from app.schemas.event import WorkflowEventListResponse
from app.services.event_service import (
    WorkflowEventService,
    format_sse_comment,
    format_sse_event,
)

router = APIRouter(tags=["events"])

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "rejected", "cancelled"}
)


def _resolve_cursor_id(after_id: int | None, last_event_id: str | None) -> int | None:
    if after_id is not None:
        return after_id
    if last_event_id is not None:
        cleaned = last_event_id.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


@router.get("/workflows/{workflow_id}/events", response_model=None)
async def get_workflow_events(
    workflow_id: str,
    request: Request,
    db: DbSession,
    _operator: CurrentOperator,
    after_id: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    stream: Annotated[bool, Query()] = False,
    follow: Annotated[bool, Query()] = True,
    poll_interval_seconds: Annotated[float, Query(ge=0.05, le=10.0)] = 0.5,
    max_duration_seconds: Annotated[float, Query(ge=0.1, le=300.0)] = 30.0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> WorkflowEventListResponse | StreamingResponse:
    """Return cursor-paginated workflow events or stream live progress via SSE."""
    service = WorkflowEventService(db)
    service.ensure_workflow_exists(workflow_id)
    cursor_id = _resolve_cursor_id(after_id, last_event_id)

    accept_header = request.headers.get("accept", "").lower()
    wants_sse = stream or ("text/event-stream" in accept_header)
    if not wants_sse:
        return service.list_events(
            workflow_id=workflow_id,
            after_id=cursor_id,
            limit=limit,
        )

    async def event_stream() -> AsyncGenerator[str, None]:
        current_cursor = cursor_id
        deadline = time.monotonic() + max_duration_seconds
        last_heartbeat = time.monotonic()

        while True:
            db.expire_all()
            batch = service.list_events(
                workflow_id=workflow_id,
                after_id=current_cursor,
                limit=limit,
            )
            for item in batch.items:
                yield format_sse_event(item)
                current_cursor = item.id

            if not follow:
                yield format_sse_comment("stream_end")
                break

            wf_status = service.get_workflow_status(workflow_id)
            if wf_status in _TERMINAL_STATUSES and not batch.has_more:
                yield format_sse_comment(f"workflow_terminal:{wf_status}")
                break

            now = time.monotonic()
            if now >= deadline or await request.is_disconnected():
                yield format_sse_comment("stream_timeout")
                break

            if not batch.items and (now - last_heartbeat) >= 5.0:
                yield format_sse_comment("keepalive")
                last_heartbeat = now

            if batch.has_more:
                continue

            await asyncio.sleep(poll_interval_seconds)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )