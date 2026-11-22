"""Authenticated REST endpoints for creating, listing, reading, and deleting projects."""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentOperator, DbSession
from app.schemas.project import ProjectCreate, ProjectListResponse, ProjectRead
from app.services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    db: DbSession,
    operator: CurrentOperator,
) -> ProjectRead:
    """Create a new engineering project."""
    service = ProjectService(db)
    project = service.create_project(payload, actor=operator.username)
    return ProjectRead.model_validate(project)


@router.get("", response_model=ProjectListResponse)
def list_projects(
    db: DbSession,
    _operator: CurrentOperator,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProjectListResponse:
    """List projects with validated pagination parameters."""
    service = ProjectService(db)
    projects, total = service.list_projects(limit=limit, offset=offset)
    return ProjectListResponse(
        items=[ProjectRead.model_validate(item) for item in projects],
        total=total,
    )


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: str,
    db: DbSession,
    _operator: CurrentOperator,
) -> ProjectRead:
    """Retrieve a single project by identifier."""
    service = ProjectService(db)
    project = service.get_project(project_id)
    return ProjectRead.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    db: DbSession,
    operator: CurrentOperator,
) -> Response:
    """Delete a project and cascade removal of its connected records."""
    service = ProjectService(db)
    service.delete_project(project_id, actor=operator.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)