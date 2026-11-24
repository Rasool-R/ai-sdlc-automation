"""Authenticated REST endpoints for creating, monitoring, and controlling SDLC workflows."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentOperator, DbSession, GitHubClientDep, WorkflowDispatcherDep
from app.schemas.workflow import (
    WorkflowActionResponse,
    WorkflowApproveRequest,
    WorkflowCancelRequest,
    WorkflowCreateRequest,
    WorkflowDetailResponse,
    WorkflowListResponse,
    WorkflowRejectRequest,
    WorkflowResumeRequest,
    WorkflowTaskListResponse,
)
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])


@router.post("", response_model=WorkflowActionResponse, status_code=status.HTTP_201_CREATED)
def create_workflow(
    payload: WorkflowCreateRequest,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    operator: CurrentOperator,
) -> WorkflowActionResponse:
    """Create a version-pinned workflow run and enqueue asynchronous execution via Celery."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.create_workflow(payload, actor=operator.username)


@router.get("", response_model=WorkflowListResponse)
def list_workflows(
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    _operator: CurrentOperator,
    repository_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    workflow_status: Annotated[
        str | None,
        Query(alias="status", min_length=1, max_length=48),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkflowListResponse:
    """List workflows with optional repository/status filters and pagination."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    items, total = service.list_workflows(
        repository_id=repository_id,
        status=workflow_status,
        limit=limit,
        offset=offset,
    )
    return WorkflowListResponse(items=items, total=total)


@router.get("/{workflow_id}", response_model=WorkflowDetailResponse)
def get_workflow(
    workflow_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    _operator: CurrentOperator,
) -> WorkflowDetailResponse:
    """Retrieve workflow projection, approvals, and aggregated LLM cost/usage metrics."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.get_workflow_detail(workflow_id)


@router.post(
    "/{workflow_id}/approve",
    response_model=WorkflowActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def approve_workflow(
    workflow_id: str,
    payload: WorkflowApproveRequest,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    operator: CurrentOperator,
) -> WorkflowActionResponse:
    """Submit a hash-bound human plan or commit/PR approval and enqueue resumption."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.approve_workflow(workflow_id, payload, actor=operator.username)


@router.post(
    "/{workflow_id}/reject",
    response_model=WorkflowActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reject_workflow(
    workflow_id: str,
    payload: WorkflowRejectRequest,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    operator: CurrentOperator,
) -> WorkflowActionResponse:
    """Reject a paused workflow and enqueue clean termination."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.reject_workflow(workflow_id, payload, actor=operator.username)


@router.post(
    "/{workflow_id}/resume",
    response_model=WorkflowActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_workflow(
    workflow_id: str,
    payload: WorkflowResumeRequest,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    operator: CurrentOperator,
) -> WorkflowActionResponse:
    """Provide clarification answers and enqueue resumption of a paused workflow."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.resume_workflow(workflow_id, payload, actor=operator.username)


@router.post(
    "/{workflow_id}/cancel",
    response_model=WorkflowActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_workflow(
    workflow_id: str,
    payload: WorkflowCancelRequest,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    operator: CurrentOperator,
) -> WorkflowActionResponse:
    """Cancel an active or paused workflow and enqueue checkpoint/workspace cleanup."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.cancel_workflow(workflow_id, payload, actor=operator.username)


@router.get("/{workflow_id}/tasks", response_model=WorkflowTaskListResponse)
def list_workflow_tasks(
    workflow_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    _operator: CurrentOperator,
) -> WorkflowTaskListResponse:
    """List persisted implementation tasks and dependency edges for a workflow."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.list_workflow_tasks(workflow_id)