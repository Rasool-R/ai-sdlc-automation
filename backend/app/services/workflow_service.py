"""Application service managing workflow lifecycle, concurrency limits, and task queries."""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ResourceNotFoundError, ValidationFailedError
from app.integrations.github.client import GitHubClient
from app.models.governance import (
    AgentExecutionModel,
    ApprovalModel,
    LLMUsageRecordModel,
    TaskDependencyModel,
    TaskModel,
)
from app.models.repository import RepositoryModel, RepositoryVersionModel
from app.models.workflow import WorkflowRunModel
from app.schemas.workflow import (
    WorkflowActionResponse,
    WorkflowApprovalRead,
    WorkflowApproveRequest,
    WorkflowCancelRequest,
    WorkflowCreateRequest,
    WorkflowDetailResponse,
    WorkflowRead,
    WorkflowRejectRequest,
    WorkflowResumeRequest,
    WorkflowTaskListResponse,
    WorkflowTaskRead,
    WorkflowUsageSummaryRead,
)
from app.services.audit_service import AuditService
from app.workflows.state import HumanDecision
from app.workflows.tasks import WorkflowJobDispatcher

DEFAULT_MAX_CONCURRENT_WORKFLOWS = 10
_ACTIVE_CONCURRENCY_STATUSES: frozenset[str] = frozenset({"pending", "running"})
_PAUSED_STATUSES: frozenset[str] = frozenset(
    {"waiting_clarification", "waiting_plan_approval", "waiting_commit_approval"}
)
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "rejected", "cancelled"}
)


class WorkflowService:
    """Coordinates authenticated workflow API requests and asynchronous job dispatch."""

    def __init__(
        self,
        db: Session,
        *,
        github_client: GitHubClient,
        dispatcher: WorkflowJobDispatcher,
        max_concurrent_workflows: int = DEFAULT_MAX_CONCURRENT_WORKFLOWS,
    ) -> None:
        self._db = db
        self._github = github_client
        self._dispatcher = dispatcher
        self._max_concurrent = max_concurrent_workflows
        self._audit = AuditService(db)

    def _get_run(self, workflow_id: str) -> WorkflowRunModel:
        run = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        )
        if run is None:
            raise ResourceNotFoundError(
                "Workflow not found.",
                details={"workflow_id": workflow_id},
            )
        return run

    def _resolve_base_commit_sha(
        self,
        *,
        repo: RepositoryModel,
        target_branch: str,
        requested_sha: str | None,
    ) -> str:
        if requested_sha:
            return requested_sha.strip()

        latest_version = self._db.scalar(
            select(RepositoryVersionModel)
            .where(
                RepositoryVersionModel.repository_id == repo.id,
                RepositoryVersionModel.branch_name == target_branch,
            )
            .order_by(RepositoryVersionModel.created_at.desc())
            .limit(1)
        )
        if latest_version is not None:
            return latest_version.commit_sha

        branch_info = self._github.get_branch(
            repo.full_name,
            target_branch,
            installation_id=repo.github_installation_id,
        )
        self._db.add(
            RepositoryVersionModel(
                repository_id=repo.id,
                branch_name=target_branch,
                commit_sha=branch_info.commit_sha,
            )
        )
        self._db.flush()
        return branch_info.commit_sha

    def create_workflow(
        self,
        payload: WorkflowCreateRequest,
        *,
        actor: str,
    ) -> WorkflowActionResponse:
        """Validate budget and concurrency, create workflow projection, and enqueue worker job."""
        idempotency_key = (payload.idempotency_key or f"wf-{uuid.uuid4().hex}").strip()
        existing = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return WorkflowActionResponse(
                workflow_id=existing.id,
                status=existing.status,
                action="create",
                job_id=None,
                reused_existing=True,
            )

        active_count = int(
            self._db.scalar(
                select(func.count())
                .select_from(WorkflowRunModel)
                .where(WorkflowRunModel.status.in_(_ACTIVE_CONCURRENCY_STATUSES))
            )
            or 0
        )
        if active_count >= self._max_concurrent:
            raise ConflictError(
                f"Maximum concurrent active workflows limit ({self._max_concurrent}) reached.",
                details={
                    "active_workflows": active_count,
                    "max_concurrent_workflows": self._max_concurrent,
                },
            )

        repo = self._db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == payload.repository_id)
        )
        if repo is None:
            raise ResourceNotFoundError(
                "Repository not found.",
                details={"repository_id": payload.repository_id},
            )

        target_branch = (payload.base_branch or repo.default_branch).strip()
        base_commit_sha = self._resolve_base_commit_sha(
            repo=repo,
            target_branch=target_branch,
            requested_sha=payload.base_commit_sha,
        )

        run = WorkflowRunModel(
            repository_id=repo.id,
            base_branch=target_branch,
            base_commit_sha=base_commit_sha,
            thread_id=f"wf-{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            title=payload.title.strip(),
            request_text=payload.request_text.strip(),
            status="pending",
            current_node="queued",
            cost_limit_usd=payload.cost_limit_usd,
            created_by=actor,
        )
        self._db.add(run)
        self._db.flush()

        job_id = self._dispatcher.enqueue_start(run.id)
        self._audit.record(
            actor=actor,
            action="workflow.enqueue",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "repository_id": repo.id,
                "base_branch": target_branch,
                "base_commit_sha": base_commit_sha,
                "cost_limit_usd": str(payload.cost_limit_usd),
                "job_id": job_id,
            },
        )
        return WorkflowActionResponse(
            workflow_id=run.id,
            status=run.status,
            action="create",
            job_id=job_id,
            reused_existing=False,
        )

    def list_workflows(
        self,
        *,
        repository_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowRead], int]:
        """Return paginated workflow run projections with optional filters."""
        filters = []
        if repository_id:
            filters.append(WorkflowRunModel.repository_id == repository_id)
        if status:
            filters.append(WorkflowRunModel.status == status)

        total = int(
            self._db.scalar(
                select(func.count()).select_from(WorkflowRunModel).where(*filters)
            )
            or 0
        )
        rows = list(
            self._db.scalars(
                select(WorkflowRunModel)
                .where(*filters)
                .order_by(WorkflowRunModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return [WorkflowRead.model_validate(r) for r in rows], total

    def get_workflow_detail(self, workflow_id: str) -> WorkflowDetailResponse:
        """Return detailed workflow projection with persisted approvals and cost metrics."""
        run = self._get_run(workflow_id)
        steps_recorded = int(
            self._db.scalar(
                select(func.count())
                .select_from(AgentExecutionModel)
                .where(AgentExecutionModel.workflow_id == run.id)
            )
            or 0
        )
        tasks_count = int(
            self._db.scalar(
                select(func.count())
                .select_from(TaskModel)
                .where(TaskModel.workflow_id == run.id)
            )
            or 0
        )
        approvals = list(
            self._db.scalars(
                select(ApprovalModel)
                .where(ApprovalModel.workflow_id == run.id)
                .order_by(ApprovalModel.decided_at.asc())
            ).all()
        )
        usage_rows = list(
            self._db.scalars(
                select(LLMUsageRecordModel).where(LLMUsageRecordModel.workflow_id == run.id)
            ).all()
        )

        total_in = sum(r.input_tokens or 0 for r in usage_rows)
        total_out = sum(r.output_tokens or 0 for r in usage_rows)
        total_tok = sum(r.total_tokens or 0 for r in usage_rows)
        total_cost = sum(
            (r.estimated_cost_usd for r in usage_rows if r.estimated_cost_usd is not None),
            Decimal("0.000000"),
        )
        all_estimated = bool(usage_rows) and all(r.cost_status == "estimated" for r in usage_rows)
        redacted_total = sum(r.secrets_redacted for r in usage_rows)

        base_data = WorkflowRead.model_validate(run).model_dump()
        return WorkflowDetailResponse(
            **base_data,
            steps_recorded=steps_recorded,
            tasks_count=tasks_count,
            approvals=[WorkflowApprovalRead.model_validate(a) for a in approvals],
            usage_summary=WorkflowUsageSummaryRead(
                llm_calls_count=len(usage_rows),
                total_input_tokens=total_in,
                total_output_tokens=total_out,
                total_tokens=total_tok,
                estimated_cost_usd=total_cost.quantize(Decimal("0.000001")),
                cost_status="estimated" if all_estimated else "unavailable",
                secrets_redacted_total=redacted_total,
            ),
        )

    def approve_workflow(
        self,
        workflow_id: str,
        payload: WorkflowApproveRequest,
        *,
        actor: str,
    ) -> WorkflowActionResponse:
        """Validate approval binding and enqueue background resumption."""
        run = self._get_run(workflow_id)
        if run.status not in {"waiting_plan_approval", "waiting_commit_approval"}:
            raise ConflictError(
                f"Workflow is not awaiting approval (current status: '{run.status}').",
                details={"workflow_id": workflow_id, "status": run.status},
            )

        if payload.approved_base_commit_sha != run.base_commit_sha:
            raise ConflictError(
                "Approval base_commit_sha does not match the workflow's pinned base SHA.",
                details={
                    "expected_base_commit_sha": run.base_commit_sha,
                    "provided_base_commit_sha": payload.approved_base_commit_sha,
                },
            )

        if run.status == "waiting_plan_approval" and not payload.approved_plan_hash:
            raise ValidationFailedError(
                "approved_plan_hash is required to approve a workflow awaiting plan approval.",
                details={"workflow_id": workflow_id},
            )
        if run.status == "waiting_commit_approval" and not payload.approved_patch_hash:
            raise ValidationFailedError(
                "approved_patch_hash is required to approve a workflow " \
                "awaiting commit/PR approval.",
                details={"workflow_id": workflow_id},
            )

        decision = HumanDecision(
            action="approve",
            actor=actor,
            approved_plan_hash=payload.approved_plan_hash,
            approved_patch_hash=payload.approved_patch_hash,
            approved_base_commit_sha=payload.approved_base_commit_sha,
            approved_protected_paths=payload.approved_protected_paths,
        )
        job_id = self._dispatcher.enqueue_resume(run.id, decision)

        self._audit.record(
            actor=actor,
            action="workflow.approve.enqueued",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "status_before": run.status,
                "approved_base_commit_sha": payload.approved_base_commit_sha,
                "approved_plan_hash": payload.approved_plan_hash,
                "approved_patch_hash": payload.approved_patch_hash,
                "job_id": job_id,
            },
        )
        return WorkflowActionResponse(
            workflow_id=run.id,
            status=run.status,
            action="approve",
            job_id=job_id,
        )

    def reject_workflow(
        self,
        workflow_id: str,
        payload: WorkflowRejectRequest,
        *,
        actor: str,
    ) -> WorkflowActionResponse:
        """Enqueue operator rejection for a paused workflow."""
        run = self._get_run(workflow_id)
        if run.status not in _PAUSED_STATUSES:
            raise ConflictError(
                f"Workflow is not paused for human decision (current status: '{run.status}').",
                details={"workflow_id": workflow_id, "status": run.status},
            )

        decision = HumanDecision(
            action="reject",
            actor=actor,
            reason=payload.reason,
        )
        job_id = self._dispatcher.enqueue_resume(run.id, decision)

        self._audit.record(
            actor=actor,
            action="workflow.reject.enqueued",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "status_before": run.status,
                "reason": payload.reason,
                "job_id": job_id,
            },
        )
        return WorkflowActionResponse(
            workflow_id=run.id,
            status=run.status,
            action="reject",
            job_id=job_id,
        )

    def resume_workflow(
        self,
        workflow_id: str,
        payload: WorkflowResumeRequest,
        *,
        actor: str,
    ) -> WorkflowActionResponse:
        """Enqueue clarification answers to resume a workflow paused for input."""
        run = self._get_run(workflow_id)
        if run.status != "waiting_clarification":
            raise ConflictError(
                f"Workflow is not awaiting clarification (current status: '{run.status}').",
                details={"workflow_id": workflow_id, "status": run.status},
            )

        decision = HumanDecision(
            action="clarify",
            actor=actor,
            clarification_answers=payload.clarification_answers,
        )
        job_id = self._dispatcher.enqueue_resume(run.id, decision)

        self._audit.record(
            actor=actor,
            action="workflow.resume.enqueued",
            resource_type="workflow",
            resource_id=run.id,
            details={"job_id": job_id},
        )
        return WorkflowActionResponse(
            workflow_id=run.id,
            status=run.status,
            action="resume",
            job_id=job_id,
        )

    def cancel_workflow(
        self,
        workflow_id: str,
        payload: WorkflowCancelRequest,
        *,
        actor: str,
    ) -> WorkflowActionResponse:
        """Mark workflow cancelled and enqueue checkpoint/workspace cleanup."""
        run = self._get_run(workflow_id)
        if run.status in _TERMINAL_STATUSES:
            raise ConflictError(
                f"Workflow in terminal status '{run.status}' cannot be cancelled.",
                details={"workflow_id": workflow_id, "status": run.status},
            )

        previous_status = run.status
        run.status = "cancelled"
        self._db.flush()

        job_id = self._dispatcher.enqueue_cancel(run.id, actor, payload.reason)
        self._audit.record(
            actor=actor,
            action="workflow.cancel.enqueued",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "previous_status": previous_status,
                "reason": payload.reason,
                "job_id": job_id,
            },
        )
        return WorkflowActionResponse(
            workflow_id=run.id,
            status="cancelled",
            action="cancel",
            job_id=job_id,
        )

    def _serialize_tasks(
        self,
        workflow_id: str,
        task_rows: list[TaskModel],
    ) -> list[WorkflowTaskRead]:
        if not task_rows:
            return []

        key_by_id = {t.id: t.task_key for t in task_rows}
        edge_rows = list(
            self._db.scalars(
                select(TaskDependencyModel).where(
                    TaskDependencyModel.workflow_id == workflow_id
                )
            ).all()
        )
        deps_by_task: dict[str, list[str]] = {}
        for edge in edge_rows:
            deps_by_task.setdefault(edge.task_id, []).append(edge.depends_on_task_id)

        result: list[WorkflowTaskRead] = []
        for row in task_rows:
            dep_ids = sorted(deps_by_task.get(row.id, []))
            dep_keys = [key_by_id[dep_id] for dep_id in dep_ids if dep_id in key_by_id]
            result.append(
                WorkflowTaskRead.model_validate(row).model_copy(
                    update={
                        "depends_on_task_ids": dep_ids,
                        "depends_on_task_keys": dep_keys,
                    }
                )
            )
        return result

    def list_workflow_tasks(self, workflow_id: str) -> WorkflowTaskListResponse:
        """List all persisted plan tasks and their dependency edges for a workflow."""
        self._get_run(workflow_id)
        rows = list(
            self._db.scalars(
                select(TaskModel)
                .where(TaskModel.workflow_id == workflow_id)
                .order_by(TaskModel.task_key.asc())
            ).all()
        )
        items = self._serialize_tasks(workflow_id, rows)
        return WorkflowTaskListResponse(workflow_id=workflow_id, items=items, total=len(items))

    def get_task(self, task_id: str) -> WorkflowTaskRead:
        """Retrieve a single persisted task with its resolved dependencies."""
        task = self._db.scalar(select(TaskModel).where(TaskModel.id == task_id))
        if task is None:
            raise ResourceNotFoundError(
                "Task not found.",
                details={"task_id": task_id},
            )

        sibling_rows = list(
            self._db.scalars(
                select(TaskModel).where(TaskModel.workflow_id == task.workflow_id)
            ).all()
        )
        serialized = self._serialize_tasks(task.workflow_id, sibling_rows)
        for item in serialized:
            if item.id == task_id:
                return item
        raise ResourceNotFoundError("Task not found.", details={"task_id": task_id})