"""Authenticated REST endpoints for inspecting Pull Requests and live CI/review status."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import (
    ArtifactStorageDep,
    CurrentOperator,
    DbSession,
    GitHubClientDep,
    WorkspaceServiceDep,
)
from app.schemas.delivery import PullRequestListResponse, PullRequestStatusResponse
from app.services.artifact_storage import ArtifactService
from app.services.delivery_service import ReleaseDeliveryService

router = APIRouter(prefix="/pull-requests", tags=["pull-requests"])


@router.get("", response_model=PullRequestListResponse)
def list_pull_requests(
    db: DbSession,
    github_client: GitHubClientDep,
    workspace_service: WorkspaceServiceDep,
    artifact_storage: ArtifactStorageDep,
    _operator: CurrentOperator,
    workflow_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    repository_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    state: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PullRequestListResponse:
    """List persisted Pull Requests created by workflows with optional filtering."""
    service = ReleaseDeliveryService(
        db,
        github_client=github_client,
        workspace_service=workspace_service,
        artifact_service=ArtifactService(db, storage=artifact_storage),
    )
    items, total = service.list_pull_requests(
        workflow_id=workflow_id,
        repository_id=repository_id,
        state=state,
        limit=limit,
        offset=offset,
    )
    return PullRequestListResponse(items=items, total=total)


@router.get("/{pull_request_id}", response_model=PullRequestStatusResponse)
def get_pull_request(
    pull_request_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    workspace_service: WorkspaceServiceDep,
    artifact_storage: ArtifactStorageDep,
    _operator: CurrentOperator,
    refresh: Annotated[bool, Query()] = False,
) -> PullRequestStatusResponse:
    """Retrieve Pull Request details and optionally refresh live GitHub CI/review status."""
    service = ReleaseDeliveryService(
        db,
        github_client=github_client,
        workspace_service=workspace_service,
        artifact_service=ArtifactService(db, storage=artifact_storage),
    )
    return service.get_pull_request(pull_request_id, refresh=refresh)