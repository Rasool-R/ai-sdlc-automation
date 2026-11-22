"""Domain service for connecting authorized repositories and recording immutable base SHAs."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ResourceNotFoundError, ValidationFailedError
from app.integrations.github.client import GitHubClient
from app.models.project import ProjectModel
from app.models.repository import RepositoryModel, RepositoryVersionModel
from app.schemas.repository import (
    RepositoryConnectRequest,
    RepositoryRead,
    RepositoryVersionRead,
)
from app.services.audit_service import AuditService


class RepositoryService:
    """Connects authorized remote repositories and tracks immutable base commit versions."""

    def __init__(self, db: Session, github_client: GitHubClient) -> None:
        self._db = db
        self._github = github_client
        self._audit = AuditService(db)

    def _get_latest_version(self, repository_id: str) -> RepositoryVersionModel | None:
        return self._db.scalar(
            select(RepositoryVersionModel)
            .where(RepositoryVersionModel.repository_id == repository_id)
            .order_by(RepositoryVersionModel.created_at.desc())
            .limit(1)
        )

    def _serialize_repository(
        self,
        repo: RepositoryModel,
        latest_version: RepositoryVersionModel | None,
    ) -> RepositoryRead:
        version_read = (
            RepositoryVersionRead.model_validate(latest_version)
            if latest_version is not None
            else None
        )
        return RepositoryRead.model_validate(repo).model_copy(
            update={"latest_version": version_read}
        )

    def connect_repository(
        self,
        payload: RepositoryConnectRequest,
        *,
        actor: str,
    ) -> RepositoryRead:
        """Verify GitHub authorization, persist repository metadata, and pin base commit SHA."""
        project = self._db.scalar(
            select(ProjectModel).where(ProjectModel.id == payload.project_id)
        )
        if project is None:
            raise ResourceNotFoundError(
                "Target project not found.",
                details={"project_id": payload.project_id},
            )

        normalized_full_name = payload.full_name.strip()
        existing = self._db.scalar(
            select(RepositoryModel).where(
                RepositoryModel.project_id == payload.project_id,
                RepositoryModel.full_name == normalized_full_name,
            )
        )
        if existing is not None:
            raise ConflictError(
                "Repository is already connected to this project.",
                details={
                    "project_id": payload.project_id,
                    "full_name": normalized_full_name,
                },
            )

        repo_info = self._github.get_repository(
            normalized_full_name,
            installation_id=payload.github_installation_id,
        )
        if repo_info.disabled:
            raise ValidationFailedError(
                "Cannot connect a disabled GitHub repository.",
                details={"full_name": repo_info.full_name},
            )

        target_branch = (payload.default_branch or repo_info.default_branch).strip()
        branch_info = self._github.get_branch(
            repo_info.full_name,
            target_branch,
            installation_id=payload.github_installation_id,
        )
        commit_info = self._github.get_commit(
            repo_info.full_name,
            branch_info.commit_sha,
            installation_id=payload.github_installation_id,
        )

        repo = RepositoryModel(
            project_id=payload.project_id,
            name=repo_info.name,
            full_name=repo_info.full_name,
            provider=payload.provider,
            clone_url=repo_info.clone_url,
            default_branch=target_branch,
            github_installation_id=payload.github_installation_id,
            is_private=repo_info.is_private,
            llm_data_sharing=payload.llm_data_sharing,
            is_active=True,
        )
        self._db.add(repo)
        self._db.flush()

        version = RepositoryVersionModel(
            repository_id=repo.id,
            branch_name=target_branch,
            commit_sha=commit_info.sha,
            commit_message=commit_info.message,
            author_name=commit_info.author_name,
            committed_at=commit_info.committed_at,
        )
        self._db.add(version)
        self._db.flush()

        self._audit.record(
            actor=actor,
            action="repository.connect",
            resource_type="repository",
            resource_id=repo.id,
            details={
                "project_id": repo.project_id,
                "full_name": repo.full_name,
                "branch": target_branch,
                "base_commit_sha": commit_info.sha,
                "is_private": repo.is_private,
                "llm_data_sharing": repo.llm_data_sharing,
            },
        )
        return self._serialize_repository(repo, version)

    def list_repositories(
        self,
        *,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RepositoryRead], int]:
        """Return paginated connected repositories with their latest recorded base version."""
        count_stmt = select(func.count()).select_from(RepositoryModel)
        rows_stmt = select(RepositoryModel).order_by(RepositoryModel.created_at.desc())

        if project_id is not None:
            count_stmt = count_stmt.where(RepositoryModel.project_id == project_id)
            rows_stmt = rows_stmt.where(RepositoryModel.project_id == project_id)

        total = int(self._db.scalar(count_stmt) or 0)
        repos = list(self._db.scalars(rows_stmt.limit(limit).offset(offset)).all())
        items = [
            self._serialize_repository(repo, self._get_latest_version(repo.id))
            for repo in repos
        ]
        return items, total

    def get_repository(self, repository_id: str) -> RepositoryRead:
        """Retrieve a connected repository and its latest pinned version by ID."""
        repo = self._db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == repository_id)
        )
        if repo is None:
            raise ResourceNotFoundError(
                "Repository not found.",
                details={"repository_id": repository_id},
            )
        latest_version = self._get_latest_version(repo.id)
        return self._serialize_repository(repo, latest_version)