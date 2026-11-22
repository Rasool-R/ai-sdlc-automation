"""Domain service for managing engineering projects with audit logging."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ResourceNotFoundError
from app.models.project import ProjectModel
from app.schemas.project import ProjectCreate
from app.services.audit_service import AuditService


class ProjectService:
    """Encapsulates project persistence, uniqueness validation, and audit records."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditService(db)

    def create_project(self, payload: ProjectCreate, *, actor: str) -> ProjectModel:
        """Create a new project after verifying name uniqueness."""
        normalized_name = payload.name.strip()
        existing = self._db.scalar(
            select(ProjectModel).where(ProjectModel.name == normalized_name)
        )
        if existing is not None:
            raise ConflictError(
                "A project with this name already exists.",
                details={"name": normalized_name},
            )

        project = ProjectModel(
            name=normalized_name,
            description=payload.description,
        )
        self._db.add(project)
        self._db.flush()

        self._audit.record(
            actor=actor,
            action="project.create",
            resource_type="project",
            resource_id=project.id,
            details={"name": project.name},
        )
        return project

    def list_projects(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ProjectModel], int]:
        """Return a paginated list of projects and the total count."""
        total = int(self._db.scalar(select(func.count()).select_from(ProjectModel)) or 0)
        rows = list(
            self._db.scalars(
                select(ProjectModel)
                .order_by(ProjectModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return rows, total

    def get_project(self, project_id: str) -> ProjectModel:
        """Retrieve a single project by ID or raise ResourceNotFoundError."""
        project = self._db.scalar(select(ProjectModel).where(ProjectModel.id == project_id))
        if project is None:
            raise ResourceNotFoundError(
                "Project not found.",
                details={"project_id": project_id},
            )
        return project

    def delete_project(self, project_id: str, *, actor: str) -> None:
        """Delete a project and record an audit entry."""
        project = self.get_project(project_id)
        project_name = project.name
        self._db.delete(project)
        self._db.flush()

        self._audit.record(
            actor=actor,
            action="project.delete",
            resource_type="project",
            resource_id=project_id,
            details={"name": project_name},
        )