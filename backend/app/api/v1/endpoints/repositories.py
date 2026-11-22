"""Authenticated REST endpoints for connecting, indexing, and inspecting repositories."""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    CurrentOperator,
    DbSession,
    EmbeddingClientDep,
    GitHubClientDep,
    QdrantClientDep,
    WorkspaceServiceDep,
)
from app.schemas.repository import (
    RepositoryConnectRequest,
    RepositoryIndexRequest,
    RepositoryIndexResponse,
    RepositoryListResponse,
    RepositoryRead,
    RepositoryStructureResponse,
)
from app.services.intelligence_service import RepositoryIntelligenceService
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


@router.post("/{repository_id}/index", response_model=RepositoryIndexResponse)
def index_repository(
    repository_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    workspace_service: WorkspaceServiceDep,
    embedding_client: EmbeddingClientDep,
    qdrant_client: QdrantClientDep,
    operator: CurrentOperator,
    payload: RepositoryIndexRequest | None = None,
) -> RepositoryIndexResponse:
    """Index a connected repository at a pinned commit SHA into structural and semantic stores."""
    service = RepositoryIntelligenceService(
        db,
        github_client=github_client,
        workspace_service=workspace_service,
        embedding_client=embedding_client,
        qdrant_client=qdrant_client,
    )
    return service.index_repository(
        repository_id,
        payload or RepositoryIndexRequest(),
        actor=operator.username,
    )


@router.get("/{repository_id}/structure", response_model=RepositoryStructureResponse)
def get_repository_structure(
    repository_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    workspace_service: WorkspaceServiceDep,
    embedding_client: EmbeddingClientDep,
    qdrant_client: QdrantClientDep,
    _operator: CurrentOperator,
    commit_sha: Annotated[str | None, Query(min_length=7, max_length=64)] = None,
    file_path: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    kind: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RepositoryStructureResponse:
    """Retrieve indexed Python symbols and dependency edges for a repository snapshot."""
    service = RepositoryIntelligenceService(
        db,
        github_client=github_client,
        workspace_service=workspace_service,
        embedding_client=embedding_client,
        qdrant_client=qdrant_client,
    )
    return service.get_repository_structure(
        repository_id,
        commit_sha=commit_sha,
        file_path=file_path,
        kind=kind,
        limit=limit,
        offset=offset,
    )