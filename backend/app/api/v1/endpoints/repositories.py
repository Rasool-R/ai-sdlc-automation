"""Authenticated REST endpoints for connecting and inspecting GitHub repositories."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentOperator, DbSession, GitHubClientDep
from app.schemas.repository import (
    RepositoryConnectRequest,
    RepositoryListResponse,
    RepositoryRead,
)
from app.services.repository_service import RepositoryService

router = APIRouter(prefix="/repositories", tags=["repositories"])


@router.post("/connect", response_model=RepositoryRead, status_code=status.HTTP_201_CREATED)
def connect_repository(
    payload: RepositoryConnectRequest,
    db: DbSession,
    github_client: GitHubClientDep,
    operator: CurrentOperator,
) -> RepositoryRead:
    """Connect an authorized GitHub repository and record its immutable base commit SHA."""
    service = RepositoryService(db, github_client)
    return service.connect_repository(payload, actor=operator.username)


@router.get("", response_model=RepositoryListResponse)
def list_repositories(
    db: DbSession,
    github_client: GitHubClientDep,
    _operator: CurrentOperator,
    project_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RepositoryListResponse:
    """List connected repositories, optionally filtered by project."""
    service = RepositoryService(db, github_client)
    items, total = service.list_repositories(
        project_id=project_id,
        limit=limit,
        offset=offset,
    )
    return RepositoryListResponse(items=items, total=total)


@router.get("/{repository_id}", response_model=RepositoryRead)
def get_repository(
    repository_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    _operator: CurrentOperator,
) -> RepositoryRead:
    """Retrieve metadata and latest pinned version for a connected repository."""
    service = RepositoryService(db, github_client)
    return service.get_repository(repository_id)