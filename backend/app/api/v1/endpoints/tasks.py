"""Authenticated REST endpoint for inspecting an individual workflow task."""

from fastapi import APIRouter

from app.api.deps import CurrentOperator, DbSession, GitHubClientDep, WorkflowDispatcherDep
from app.schemas.workflow import WorkflowTaskRead
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=WorkflowTaskRead)
def get_task(
    task_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    dispatcher: WorkflowDispatcherDep,
    _operator: CurrentOperator,
) -> WorkflowTaskRead:
    """Retrieve a single persisted workflow task and its dependency edges."""
    service = WorkflowService(db, github_client=github_client, dispatcher=dispatcher)
    return service.get_task(task_id)