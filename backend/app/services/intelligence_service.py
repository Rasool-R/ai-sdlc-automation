"""Orchestration service for repository indexing and structural inspection endpoints."""

import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ResourceNotFoundError
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.github.client import GitHubClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.indexer import RepositoryIndexer
from app.models.intelligence import (
    CodeRelationshipModel,
    CodeSymbolModel,
    RepositoryIndexSnapshotModel,
)
from app.models.repository import RepositoryModel, RepositoryVersionModel
from app.schemas.repository import (
    IndexedRelationshipRead,
    IndexedSymbolRead,
    RepositoryIndexRequest,
    RepositoryIndexResponse,
    RepositoryStructureResponse,
)
from app.services.audit_service import AuditService
from app.services.lock_service import RepositoryLockService
from app.services.workspace_service import WorkspaceService


class RepositoryIntelligenceService:
    """Coordinates workspace locking, cloning, incremental indexing, and structure queries."""

    def __init__(
        self,
        db: Session,
        *,
        github_client: GitHubClient,
        workspace_service: WorkspaceService,
        embedding_client: EmbeddingClient,
        qdrant_client: QdrantIndexClient,
    ) -> None:
        self._db = db
        self._github = github_client
        self._workspaces = workspace_service
        self._locks = RepositoryLockService(db)
        self._audit = AuditService(db)
        self._indexer = RepositoryIndexer(
            db,
            embedding_client=embedding_client,
            qdrant_client=qdrant_client,
        )

    def _get_repository(self, repository_id: str) -> RepositoryModel:
        repo = self._db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == repository_id)
        )
        if repo is None:
            raise ResourceNotFoundError(
                "Repository not found.",
                details={"repository_id": repository_id},
            )
        return repo

    def index_repository(
        self,
        repository_id: str,
        payload: RepositoryIndexRequest,
        *,
        actor: str,
    ) -> RepositoryIndexResponse:
        """Acquire lock, prepare versioned workspace, run indexer, and guarantee cleanup."""
        repo = self._get_repository(repository_id)
        target_branch = (payload.branch or repo.default_branch).strip()

        if payload.commit_sha:
            commit_info = self._github.get_commit(
                repo.full_name,
                payload.commit_sha.strip(),
                installation_id=repo.github_installation_id,
            )
            target_sha = commit_info.sha
        else:
            branch_info = self._github.get_branch(
                repo.full_name,
                target_branch,
                installation_id=repo.github_installation_id,
            )
            commit_info = self._github.get_commit(
                repo.full_name,
                branch_info.commit_sha,
                installation_id=repo.github_installation_id,
            )
            target_sha = commit_info.sha

        existing_version = self._db.scalar(
            select(RepositoryVersionModel).where(
                RepositoryVersionModel.repository_id == repo.id,
                RepositoryVersionModel.branch_name == target_branch,
                RepositoryVersionModel.commit_sha == target_sha,
            )
        )
        if existing_version is None:
            self._db.add(
                RepositoryVersionModel(
                    repository_id=repo.id,
                    branch_name=target_branch,
                    commit_sha=target_sha,
                    commit_message=commit_info.message,
                    author_name=commit_info.author_name,
                    committed_at=commit_info.committed_at,
                )
            )
            self._db.flush()

        run_suffix = uuid.uuid4().hex[:12]
        lock_owner = f"index:{actor}:{run_suffix}"
        workspace_id = f"idx_{repo.id}_{run_suffix}"

        self._locks.acquire(
            repository_id=repo.id,
            owner_id=lock_owner,
            operation="repository.index",
            ttl_seconds=900,
        )

        try:
            clone_source = self._github.build_authenticated_clone_url(
                repo.clone_url,
                installation_id=repo.github_installation_id,
            )
            workspace_info = self._workspaces.prepare_workspace(
                workspace_id=workspace_id,
                clone_source=clone_source,
                base_commit_sha=target_sha,
                branch_name=target_branch if not payload.commit_sha else None,
            )
            index_result = self._indexer.index_workspace(
                repository=repo,
                workspace_dir=Path(workspace_info.workspace_path),
                commit_sha=target_sha,
                force_reindex=payload.force_reindex,
            )
            combined_warnings = list(workspace_info.warnings) + list(index_result.warnings)

            self._audit.record(
                actor=actor,
                action="repository.index",
                resource_type="repository",
                resource_id=repo.id,
                details={
                    "commit_sha": target_sha,
                    "snapshot_id": index_result.snapshot_id,
                    "files_indexed_count": index_result.files_indexed_count,
                    "symbols_count": index_result.symbols_count,
                    "semantic_indexed": index_result.semantic_indexed,
                    "secrets_redacted_count": index_result.secrets_redacted_count,
                },
            )
            return RepositoryIndexResponse(
                snapshot_id=index_result.snapshot_id,
                repository_id=index_result.repository_id,
                commit_sha=index_result.commit_sha,
                status=index_result.status,
                files_indexed_count=index_result.files_indexed_count,
                symbols_count=index_result.symbols_count,
                relationships_count=index_result.relationships_count,
                parse_errors_count=index_result.parse_errors_count,
                semantic_indexed=index_result.semantic_indexed,
                reused_snapshot=index_result.reused_snapshot,
                unchanged_files_skipped=index_result.unchanged_files_skipped,
                reindexed_files=index_result.reindexed_files,
                deleted_files=index_result.deleted_files,
                secrets_redacted_count=index_result.secrets_redacted_count,
                warnings=combined_warnings,
            )
        finally:
            self._workspaces.cleanup_workspace(workspace_id)
            self._locks.release(repository_id=repo.id, owner_id=lock_owner)

    def get_repository_structure(
        self,
        repository_id: str,
        *,
        commit_sha: str | None = None,
        file_path: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> RepositoryStructureResponse:
        """Return indexed structural symbols and relationships for a repository snapshot."""
        repo = self._get_repository(repository_id)

        snapshot_stmt = select(RepositoryIndexSnapshotModel).where(
            RepositoryIndexSnapshotModel.repository_id == repo.id,
            RepositoryIndexSnapshotModel.status == "completed",
        )
        if commit_sha:
            snapshot_stmt = snapshot_stmt.where(
                RepositoryIndexSnapshotModel.commit_sha == commit_sha
            )
        else:
            snapshot_stmt = snapshot_stmt.order_by(
                RepositoryIndexSnapshotModel.created_at.desc()
            )

        snapshot = self._db.scalar(snapshot_stmt.limit(1))
        if snapshot is None:
            raise ResourceNotFoundError(
                "No completed index snapshot found for this repository.",
                details={"repository_id": repository_id, "commit_sha": commit_sha},
            )

        sym_filter = [CodeSymbolModel.snapshot_id == snapshot.id]
        rel_filter = [CodeRelationshipModel.snapshot_id == snapshot.id]
        if file_path:
            sym_filter.append(CodeSymbolModel.file_path == file_path)
            rel_filter.append(CodeRelationshipModel.file_path == file_path)
        if kind:
            sym_filter.append(CodeSymbolModel.kind == kind)

        total_symbols = int(
            self._db.scalar(select(func.count()).select_from(CodeSymbolModel).where(*sym_filter))
            or 0
        )
        total_relationships = int(
            self._db.scalar(
                select(func.count()).select_from(CodeRelationshipModel).where(*rel_filter)
            )
            or 0
        )

        sym_rows = list(
            self._db.scalars(
                select(CodeSymbolModel)
                .where(*sym_filter)
                .order_by(CodeSymbolModel.file_path.asc(), CodeSymbolModel.start_line.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        rel_rows = list(
            self._db.scalars(
                select(CodeRelationshipModel)
                .where(*rel_filter)
                .order_by(CodeRelationshipModel.file_path.asc(), CodeRelationshipModel.line.asc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

        return RepositoryStructureResponse(
            repository_id=repo.id,
            snapshot_id=snapshot.id,
            commit_sha=snapshot.commit_sha,
            files_indexed_count=snapshot.files_indexed_count,
            semantic_indexed=snapshot.semantic_indexed,
            symbols=[IndexedSymbolRead.model_validate(r) for r in sym_rows],
            relationships=[IndexedRelationshipRead.model_validate(r) for r in rel_rows],
            total_symbols=total_symbols,
            total_relationships=total_relationships,
        )