"""Background Celery tasks and dispatcher for asynchronous LangGraph execution.

Retry ownership:
- Celery retries ONLY transient infrastructure failures (database connectivity,
  broker/network errors) around idempotent checkpoint invocation.
- LLM/schema/tool retries live inside node and client code under explicit workflow budgets.
- At human interrupts, the graph checkpoints and the Celery task exits immediately,
  releasing the worker.
"""

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, ResourceNotFoundError
from app.core.logging import workflow_id_ctx
from app.db.session import SessionLocal
from app.execution.patch_service import PatchService
from app.execution.sandbox_client import SandboxClient
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.github.client import GitHubClient
from app.integrations.llm.client import LLMClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.indexer import RepositoryIndexer
from app.intelligence.retrieval import HybridRetrievalService
from app.models.intelligence import RepositoryIndexSnapshotModel
from app.models.repository import RepositoryModel
from app.models.workflow import WorkflowRunModel
from app.services.lock_service import RepositoryLockService
from app.services.workspace_service import WorkspaceService
from app.workflows.celery_app import celery_app
from app.workflows.checkpointer import postgres_checkpointer
from app.workflows.graph import WorkflowOrchestrator
from app.workflows.state import HumanDecision, WorkflowExecutionSnapshot

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "rejected", "cancelled"}
)
_INFRA_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    TimeoutError,
    OSError,
)


class _CeleryTaskProtocol(Protocol):
    """Minimal typed interface for registered Celery tasks."""

    def delay(self, *args: Any, **kwargs: Any) -> Any:
        """Enqueue the task asynchronously onto the broker."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the underlying task callable directly."""


def _register_celery_task(**kwargs: Any) -> Callable[[Callable[..., Any]], _CeleryTaskProtocol]:
    """Typed wrapper around Celery's untyped task decorator for strict mypy compliance."""
    return cast(
        Callable[[Callable[..., Any]], _CeleryTaskProtocol],
        celery_app.task(**kwargs),
    )


def workflow_workspace_id(workflow_id: str) -> str:
    """Return the deterministic isolated workspace identifier for a workflow run."""
    return f"wf_{workflow_id}"


class WorkflowJobDispatcher(Protocol):
    """Abstraction decoupling FastAPI handlers from the background queue broker."""

    def enqueue_start(self, workflow_id: str) -> str:
        """Enqueue background execution of a newly created workflow run."""

    def enqueue_resume(self, workflow_id: str, decision: HumanDecision) -> str:
        """Enqueue background resumption of a paused workflow run."""

    def enqueue_cancel(self, workflow_id: str, actor: str, reason: str | None) -> str:
        """Enqueue background checkpoint cancellation for an active/paused workflow."""


class CeleryWorkflowDispatcher:
    """Production dispatcher routing workflow operations through Celery/Redis."""

    def enqueue_start(self, workflow_id: str) -> str:
        result = run_workflow_task.delay(workflow_id)
        return str(result.id)

    def enqueue_resume(self, workflow_id: str, decision: HumanDecision) -> str:
        result = resume_workflow_task.delay(workflow_id, decision.model_dump(mode="json"))
        return str(result.id)

    def enqueue_cancel(self, workflow_id: str, actor: str, reason: str | None) -> str:
        result = cancel_workflow_task.delay(workflow_id, actor, reason)
        return str(result.id)


def _ensure_workflow_workspace(
    *,
    db: Session,
    run: WorkflowRunModel,
    repo: RepositoryModel,
    github_client: GitHubClient,
    workspace_service: WorkspaceService,
    embedding_client: EmbeddingClient,
    qdrant_client: QdrantIndexClient,
    settings: Settings,
) -> Path:
    """Ensure the isolated workspace exists at base_commit_sha and is structurally indexed."""
    ws_id = workflow_workspace_id(run.id)
    ws_path = (workspace_service.root_dir / ws_id).resolve()

    if ws_path.is_dir():
        workspace_service.verify_base_sha(
            workspace_id=ws_id,
            expected_base_sha=run.base_commit_sha,
        )
    else:
        clone_source = github_client.build_authenticated_clone_url(
            repo.clone_url,
            installation_id=repo.github_installation_id,
        )
        ws_info = workspace_service.prepare_workspace(
            workspace_id=ws_id,
            clone_source=clone_source,
            base_commit_sha=run.base_commit_sha,
            branch_name=run.base_branch,
        )
        ws_path = Path(ws_info.workspace_path)

    existing_snapshot = db.scalar(
        select(RepositoryIndexSnapshotModel).where(
            RepositoryIndexSnapshotModel.repository_id == repo.id,
            RepositoryIndexSnapshotModel.commit_sha == run.base_commit_sha,
            RepositoryIndexSnapshotModel.status == "completed",
        )
    )
    if existing_snapshot is None:
        RepositoryIndexer(
            db,
            settings=settings,
            embedding_client=embedding_client,
            qdrant_client=qdrant_client,
        ).index_workspace(
            repository=repo,
            workspace_dir=ws_path,
            commit_sha=run.base_commit_sha,
        )

    return ws_path


def _checkpointer_context(
    settings: Settings,
    override: Any | None,
) -> AbstractContextManager[Any]:
    if override is not None:
        return nullcontext(override)
    return postgres_checkpointer(settings.DATABASE_URL, auto_setup=True)


def execute_workflow_job(
    *,
    db: Session,
    workflow_id: str,
    settings: Settings | None = None,
    github_client: GitHubClient | None = None,
    workspace_service: WorkspaceService | None = None,
    llm_client: LLMClient | None = None,
    embedding_client: EmbeddingClient | None = None,
    qdrant_client: QdrantIndexClient | None = None,
    sandbox_client: SandboxClient | None = None,
    checkpointer: Any | None = None,
) -> WorkflowExecutionSnapshot:
    """Execute a queued workflow idempotently under a repository lock."""
    resolved_settings = settings or get_settings()
    run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id))
    if run is None:
        raise ResourceNotFoundError(
            "Workflow run not found for background execution.",
            details={"workflow_id": workflow_id},
        )
    repo = db.scalar(select(RepositoryModel).where(RepositoryModel.id == run.repository_id))
    if repo is None:
        raise ResourceNotFoundError(
            "Repository not found for background workflow execution.",
            details={"repository_id": run.repository_id, "workflow_id": workflow_id},
        )

    owns_github = github_client is None
    owns_llm = llm_client is None
    owns_embed = embedding_client is None
    owns_sandbox = sandbox_client is None

    gh = github_client or GitHubClient(settings=resolved_settings)
    ws = workspace_service or WorkspaceService(settings=resolved_settings)
    llm = llm_client or LLMClient(settings=resolved_settings)
    emb = embedding_client or EmbeddingClient(settings=resolved_settings)
    qdrant = qdrant_client or QdrantIndexClient(settings=resolved_settings)
    sbx = sandbox_client or SandboxClient(settings=resolved_settings)

    locks = RepositoryLockService(db)
    lock_owner = f"worker:start:{workflow_id}:{uuid.uuid4().hex[:8]}"
    ws_id = workflow_workspace_id(workflow_id)
    wf_token = workflow_id_ctx.set(workflow_id)
    lock_acquired = False
    should_cleanup_workspace = False

    try:
        locks.acquire(
            repository_id=repo.id,
            owner_id=lock_owner,
            operation="workflow.start",
            ttl_seconds=resolved_settings.WORKFLOW_TIMEOUT_SECONDS,
        )
        lock_acquired = True

        if run.status == "cancelled":
            should_cleanup_workspace = True
            with _checkpointer_context(resolved_settings, checkpointer) as saver:
                orchestrator = WorkflowOrchestrator(
                    db,
                    llm_client=llm,
                    retrieval_service=HybridRetrievalService(
                        db,
                        settings=resolved_settings,
                        embedding_client=emb,
                        qdrant_client=qdrant,
                    ),
                    patch_service=PatchService(settings=resolved_settings),
                    sandbox_client=sbx,
                    checkpointer=saver,
                    settings=resolved_settings,
                )
                return orchestrator.execute_queued_workflow(
                    workflow_id=workflow_id,
                    workspace_dir=ws.root_dir / ws_id,
                )

        ws_path = _ensure_workflow_workspace(
            db=db,
            run=run,
            repo=repo,
            github_client=gh,
            workspace_service=ws,
            embedding_client=emb,
            qdrant_client=qdrant,
            settings=resolved_settings,
        )

        with _checkpointer_context(resolved_settings, checkpointer) as saver:
            orchestrator = WorkflowOrchestrator(
                db,
                llm_client=llm,
                retrieval_service=HybridRetrievalService(
                    db,
                    settings=resolved_settings,
                    embedding_client=emb,
                    qdrant_client=qdrant,
                ),
                patch_service=PatchService(settings=resolved_settings),
                sandbox_client=sbx,
                checkpointer=saver,
                settings=resolved_settings,
            )
            snapshot = orchestrator.execute_queued_workflow(
                workflow_id=workflow_id,
                workspace_dir=ws_path,
            )

        if snapshot.status in _TERMINAL_STATUSES:
            should_cleanup_workspace = True
        return snapshot
    finally:
        if should_cleanup_workspace:
            ws.cleanup_workspace(ws_id)
        if lock_acquired:
            locks.release(repository_id=repo.id, owner_id=lock_owner)
        if owns_github:
            gh.close()
        if owns_llm:
            llm.close()
        if owns_embed:
            emb.close()
        if owns_sandbox:
            sbx.close()
        workflow_id_ctx.reset(wf_token)


def resume_workflow_job(
    *,
    db: Session,
    workflow_id: str,
    decision: HumanDecision,
    settings: Settings | None = None,
    github_client: GitHubClient | None = None,
    workspace_service: WorkspaceService | None = None,
    llm_client: LLMClient | None = None,
    embedding_client: EmbeddingClient | None = None,
    qdrant_client: QdrantIndexClient | None = None,
    sandbox_client: SandboxClient | None = None,
    checkpointer: Any | None = None,
) -> WorkflowExecutionSnapshot:
    """Resume a paused workflow from its checkpoint under a repository lock."""
    resolved_settings = settings or get_settings()
    run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id))
    if run is None:
        raise ResourceNotFoundError(
            "Workflow run not found for background resume.",
            details={"workflow_id": workflow_id},
        )
    repo = db.scalar(select(RepositoryModel).where(RepositoryModel.id == run.repository_id))
    if repo is None:
        raise ResourceNotFoundError(
            "Repository not found for background workflow resume.",
            details={"repository_id": run.repository_id, "workflow_id": workflow_id},
        )

    owns_github = github_client is None
    owns_llm = llm_client is None
    owns_embed = embedding_client is None
    owns_sandbox = sandbox_client is None

    gh = github_client or GitHubClient(settings=resolved_settings)
    ws = workspace_service or WorkspaceService(settings=resolved_settings)
    llm = llm_client or LLMClient(settings=resolved_settings)
    emb = embedding_client or EmbeddingClient(settings=resolved_settings)
    qdrant = qdrant_client or QdrantIndexClient(settings=resolved_settings)
    sbx = sandbox_client or SandboxClient(settings=resolved_settings)

    locks = RepositoryLockService(db)
    lock_owner = f"worker:resume:{workflow_id}:{uuid.uuid4().hex[:8]}"
    ws_id = workflow_workspace_id(workflow_id)
    wf_token = workflow_id_ctx.set(workflow_id)
    lock_acquired = False
    should_cleanup_workspace = False

    try:
        locks.acquire(
            repository_id=repo.id,
            owner_id=lock_owner,
            operation=f"workflow.resume.{decision.action}",
            ttl_seconds=resolved_settings.WORKFLOW_TIMEOUT_SECONDS,
        )
        lock_acquired = True

        if decision.action != "reject":
            _ensure_workflow_workspace(
                db=db,
                run=run,
                repo=repo,
                github_client=gh,
                workspace_service=ws,
                embedding_client=emb,
                qdrant_client=qdrant,
                settings=resolved_settings,
            )

        with _checkpointer_context(resolved_settings, checkpointer) as saver:
            orchestrator = WorkflowOrchestrator(
                db,
                llm_client=llm,
                retrieval_service=HybridRetrievalService(
                    db,
                    settings=resolved_settings,
                    embedding_client=emb,
                    qdrant_client=qdrant,
                ),
                patch_service=PatchService(settings=resolved_settings),
                sandbox_client=sbx,
                checkpointer=saver,
                settings=resolved_settings,
            )
            snapshot = orchestrator.resume_workflow(
                workflow_id=workflow_id,
                decision=decision,
            )

        if snapshot.status in _TERMINAL_STATUSES:
            should_cleanup_workspace = True
        return snapshot
    finally:
        if should_cleanup_workspace:
            ws.cleanup_workspace(ws_id)
        if lock_acquired:
            locks.release(repository_id=repo.id, owner_id=lock_owner)
        if owns_github:
            gh.close()
        if owns_llm:
            llm.close()
        if owns_embed:
            emb.close()
        if owns_sandbox:
            sbx.close()
        workflow_id_ctx.reset(wf_token)


def cancel_workflow_job(
    *,
    db: Session,
    workflow_id: str,
    actor: str,
    reason: str | None = None,
    settings: Settings | None = None,
    workspace_service: WorkspaceService | None = None,
    checkpointer: Any | None = None,
) -> WorkflowExecutionSnapshot:
    """Propagate workflow cancellation into the LangGraph checkpoint and clean workspace."""
    resolved_settings = settings or get_settings()
    ws = workspace_service or WorkspaceService(settings=resolved_settings)
    ws_id = workflow_workspace_id(workflow_id)
    wf_token = workflow_id_ctx.set(workflow_id)

    try:
        with _checkpointer_context(resolved_settings, checkpointer) as saver:
            orchestrator = WorkflowOrchestrator(
                db,
                llm_client=LLMClient(settings=resolved_settings),
                retrieval_service=HybridRetrievalService(db, settings=resolved_settings),
                patch_service=PatchService(settings=resolved_settings),
                sandbox_client=SandboxClient(settings=resolved_settings),
                checkpointer=saver,
                settings=resolved_settings,
            )
            snapshot = orchestrator.cancel_workflow(
                workflow_id=workflow_id,
                actor=actor,
                reason=reason,
            )
        ws.cleanup_workspace(ws_id)
        return snapshot
    finally:
        workflow_id_ctx.reset(wf_token)


@_register_celery_task(
    name="workflows.run_workflow",
    bind=True,
    acks_late=True,
    max_retries=3,
    autoretry_for=_INFRA_RETRY_EXCEPTIONS,
    retry_backoff=True,
    retry_jitter=True,
)
def run_workflow_task(self: Any, workflow_id: str) -> dict[str, Any]:
    """Celery entrypoint running a queued SDLC workflow until pause or terminal state."""
    _ = self
    db = SessionLocal()
    try:
        snapshot = execute_workflow_job(db=db, workflow_id=workflow_id)
        db.commit()
        return snapshot.model_dump(mode="json")
    except AppError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_register_celery_task(
    name="workflows.resume_workflow",
    bind=True,
    acks_late=True,
    max_retries=3,
    autoretry_for=_INFRA_RETRY_EXCEPTIONS,
    retry_backoff=True,
    retry_jitter=True,
)
def resume_workflow_task(
    self: Any,
    workflow_id: str,
    decision_payload: dict[str, Any],
) -> dict[str, Any]:
    """Celery entrypoint resuming a paused SDLC workflow from its PostgreSQL checkpoint."""
    _ = self
    decision = HumanDecision.model_validate(decision_payload)
    db = SessionLocal()
    try:
        snapshot = resume_workflow_job(db=db, workflow_id=workflow_id, decision=decision)
        db.commit()
        return snapshot.model_dump(mode="json")
    except AppError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_register_celery_task(
    name="workflows.cancel_workflow",
    bind=True,
    acks_late=True,
    max_retries=3,
    autoretry_for=_INFRA_RETRY_EXCEPTIONS,
    retry_backoff=True,
    retry_jitter=True,
)
def cancel_workflow_task(
    self: Any,
    workflow_id: str,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Celery entrypoint cancelling a workflow in its checkpoint and cleaning its workspace."""
    _ = self
    db = SessionLocal()
    try:
        snapshot = cancel_workflow_job(
            db=db,
            workflow_id=workflow_id,
            actor=actor,
            reason=reason,
        )
        db.commit()
        return snapshot.model_dump(mode="json")
    except AppError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()