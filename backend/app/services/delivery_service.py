"""Controlled Pull Request delivery, artifact bundling, and evidence-based workflow reporting.

This service never merges Pull Requests and never deploys code.
"""

import hashlib
import re
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.schemas import ReleasePreparationOutput
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from app.integrations.github.client import GitHubClient
from app.models.delivery import PullRequestModel, WorkflowArtifactModel
from app.models.execution import (
    CodeChangeModel,
    CodeReviewModel,
    StaticAnalysisFindingModel,
    TestExecutionModel,
)
from app.models.governance import ApprovalModel, LLMUsageRecordModel
from app.models.repository import RepositoryModel
from app.models.workflow import WorkflowRunModel
from app.schemas.delivery import (
    ArtifactRead,
    PullRequestDeliveryResult,
    PullRequestRead,
    PullRequestStatusResponse,
    ReportCodeChangeSummary,
    ReportCodeReviewSummary,
    ReportStaticFindingSummary,
    ReportTestExecutionSummary,
    WorkflowReportResponse,
)
from app.schemas.workflow import WorkflowApprovalRead, WorkflowUsageSummaryRead
from app.services.artifact_storage import ArtifactService
from app.services.audit_service import AuditService
from app.services.workspace_service import WorkspaceService

_NON_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def build_deterministic_branch_name(workflow_id: str, title: str) -> str:
    """Build a deterministic, safe feature branch name anchored to workflow_id."""
    short_id = re.sub(r"[^a-zA-Z0-9]", "", workflow_id)[:8].lower() or "wf"
    slug = _NON_SLUG_PATTERN.sub("-", title.lower()).strip("-")[:40].strip("-")
    if not slug:
        slug = "change"
    return f"ai-sdlc/{short_id}-{slug}"


def build_pr_operation_id(
    *,
    workflow_id: str,
    base_commit_sha: str,
    patch_hash: str,
) -> str:
    """Compute a deterministic idempotency key for branch/commit/PR delivery."""
    digest = hashlib.sha256(
        f"{workflow_id}:{base_commit_sha}:{patch_hash}".encode()
    ).hexdigest()
    return f"pr-{digest[:40]}"


class ReleaseDeliveryService:
    """Coordinates idempotent branch push, PR creation, artifact storage, and reporting."""

    def __init__(
        self,
        db: Session,
        *,
        github_client: GitHubClient,
        workspace_service: WorkspaceService,
        artifact_service: ArtifactService,
    ) -> None:
        self._db = db
        self._github = github_client
        self._workspace = workspace_service
        self._artifacts = artifact_service
        self._audit = AuditService(db)

    def persist_workflow_artifacts(
        self,
        *,
        workflow_id: str,
        created_by: str,
        unified_diff: str | None = None,
        sandbox_report: dict[str, Any] | None = None,
        static_analysis_report: dict[str, Any] | None = None,
        code_review_report: dict[str, Any] | None = None,
        release_output: dict[str, Any] | None = None,
    ) -> list[str]:
        """Store versioned, secret-redacted workflow artifacts and return their IDs."""
        stored_ids: list[str] = []

        if unified_diff:
            art = self._artifacts.store_text_artifact(
                workflow_id=workflow_id,
                artifact_type="unified_diff",
                name="changes.diff",
                text_content=unified_diff,
                content_type="text/x-diff",
                created_by=created_by,
            )
            stored_ids.append(art.id)

        if isinstance(sandbox_report, dict):
            art = self._artifacts.store_json_artifact(
                workflow_id=workflow_id,
                artifact_type="test_report",
                name="sandbox_test_report.json",
                payload=sandbox_report,
                created_by=created_by,
            )
            stored_ids.append(art.id)

        if isinstance(static_analysis_report, dict):
            art = self._artifacts.store_json_artifact(
                workflow_id=workflow_id,
                artifact_type="static_analysis_report",
                name="static_analysis_report.json",
                payload=static_analysis_report,
                created_by=created_by,
            )
            stored_ids.append(art.id)

        if isinstance(code_review_report, dict):
            art = self._artifacts.store_json_artifact(
                workflow_id=workflow_id,
                artifact_type="code_review_report",
                name="code_review_report.json",
                payload=code_review_report,
                created_by=created_by,
            )
            stored_ids.append(art.id)

        if isinstance(release_output, dict):
            markdown = str(release_output.get("final_report_markdown") or "").strip()
            if markdown:
                art = self._artifacts.store_text_artifact(
                    workflow_id=workflow_id,
                    artifact_type="final_report",
                    name="final_report.md",
                    text_content=markdown,
                    content_type="text/markdown",
                    created_by=created_by,
                )
                stored_ids.append(art.id)

        return stored_ids

    def deliver_pull_request(
        self,
        *,
        workflow_id: str,
        workspace_id: str,
        base_commit_sha: str,
        patch_hash: str,
        commit_approved: bool,
        release_output: ReleasePreparationOutput,
        actor: str,
        branch_name: str | None = None,
        draft: bool = False,
    ) -> PullRequestDeliveryResult:
        """Idempotently push an approved feature branch and open a GitHub Pull Request."""
        if not commit_approved:
            raise AuthorizationError(
                "Commit/PR human approval is mandatory before pushing or opening a Pull Request.",
                details={"workflow_id": workflow_id},
            )

        if not patch_hash.strip():
            raise ValidationFailedError(
                "A validated patch_hash is required to deliver a Pull Request.",
                details={"workflow_id": workflow_id},
            )

        operation_id = build_pr_operation_id(
            workflow_id=workflow_id,
            base_commit_sha=base_commit_sha,
            patch_hash=patch_hash,
        )
        existing_pr = self._db.scalar(
            select(PullRequestModel).where(PullRequestModel.operation_id == operation_id)
        )
        if existing_pr is not None:
            return PullRequestDeliveryResult(
                pull_request_id=existing_pr.id,
                pr_number=existing_pr.pr_number,
                title=existing_pr.title,
                html_url=existing_pr.html_url,
                head_branch=existing_pr.head_branch,
                head_commit_sha=existing_pr.head_commit_sha,
                base_branch=existing_pr.base_branch,
                base_commit_sha=existing_pr.base_commit_sha,
                patch_hash=existing_pr.patch_hash,
                operation_id=existing_pr.operation_id,
                reused_existing=True,
            )

        run = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        )
        if run is None:
            raise ResourceNotFoundError(
                "Workflow run not found for Pull Request delivery.",
                details={"workflow_id": workflow_id},
            )
        if run.base_commit_sha != base_commit_sha:
            raise ConflictError(
                "Workflow base_commit_sha does not match delivery base_commit_sha.",
                details={
                    "expected_base_commit_sha": run.base_commit_sha,
                    "provided_base_commit_sha": base_commit_sha,
                },
            )

        repo = self._db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == run.repository_id)
        )
        if repo is None:
            raise ResourceNotFoundError(
                "Repository not found for Pull Request delivery.",
                details={"repository_id": run.repository_id},
            )

        # Verify remote base branch has not drifted before writing
        remote_base = self._github.get_branch(
            repo.full_name,
            run.base_branch,
            installation_id=repo.github_installation_id,
        )
        self._workspace.detect_stale_base(
            expected_base_sha=base_commit_sha,
            current_remote_sha=remote_base.commit_sha,
            branch_name=run.base_branch,
        )

        target_branch = (
            branch_name.strip()
            if branch_name and branch_name.strip()
            else build_deterministic_branch_name(workflow_id, release_output.pr_title)
        )
        remote_push_url = self._github.build_authenticated_clone_url(
            repo.clone_url,
            installation_id=repo.github_installation_id,
        )

        commit_message = (
            f"{release_output.pr_title}\n\n"
            f"Workflow-ID: {workflow_id}\n"
            f"Base-SHA: {base_commit_sha}\n"
            f"Patch-Hash: {patch_hash}"
        )
        git_result = self._workspace.create_branch_commit_and_push(
            workspace_id=workspace_id,
            branch_name=target_branch,
            base_branch=run.base_branch,
            expected_base_sha=base_commit_sha,
            commit_message=commit_message,
            remote_push_url=remote_push_url,
        )

        pr_info, pr_reused = self._github.create_pull_request(
            repo.full_name,
            title=release_output.pr_title,
            body=release_output.pr_description,
            head_branch=git_result.branch_name,
            base_branch=run.base_branch,
            draft=draft,
            installation_id=repo.github_installation_id,
        )

        existing_by_number = self._db.scalar(
            select(PullRequestModel).where(
                PullRequestModel.repository_id == repo.id,
                PullRequestModel.pr_number == pr_info.number,
            )
        )
        if existing_by_number is not None:
            existing_by_number.head_commit_sha = git_result.commit_sha
            existing_by_number.state = pr_info.state
            self._db.flush()
            pr_row = existing_by_number
        else:
            pr_row = PullRequestModel(
                workflow_id=run.id,
                repository_id=repo.id,
                operation_id=operation_id,
                provider=repo.provider,
                pr_number=pr_info.number,
                title=pr_info.title,
                body=pr_info.body,
                head_branch=git_result.branch_name,
                base_branch=run.base_branch,
                base_commit_sha=base_commit_sha,
                head_commit_sha=git_result.commit_sha,
                patch_hash=patch_hash,
                html_url=pr_info.html_url,
                state=pr_info.state,
                ci_status="unknown",
                review_decision="pending",
                created_by=actor,
            )
            self._db.add(pr_row)
            self._db.flush()

        self._audit.record(
            actor=actor,
            action="pull_request.deliver",
            resource_type="pull_request",
            resource_id=pr_row.id,
            details={
                "workflow_id": run.id,
                "repository": repo.full_name,
                "pr_number": pr_row.pr_number,
                "head_branch": pr_row.head_branch,
                "base_branch": pr_row.base_branch,
                "base_commit_sha": base_commit_sha,
                "head_commit_sha": git_result.commit_sha,
                "patch_hash": patch_hash,
                "operation_id": operation_id,
                "reused_existing": pr_reused,
            },
        )

        return PullRequestDeliveryResult(
            pull_request_id=pr_row.id,
            pr_number=pr_row.pr_number,
            title=pr_row.title,
            html_url=pr_row.html_url,
            head_branch=pr_row.head_branch,
            head_commit_sha=pr_row.head_commit_sha,
            base_branch=pr_row.base_branch,
            base_commit_sha=pr_row.base_commit_sha,
            patch_hash=pr_row.patch_hash,
            operation_id=pr_row.operation_id,
            reused_existing=pr_reused,
        )

    def list_pull_requests(
        self,
        *,
        workflow_id: str | None = None,
        repository_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PullRequestRead], int]:
        """Return paginated persisted Pull Requests with optional filters."""
        filters = []
        if workflow_id:
            filters.append(PullRequestModel.workflow_id == workflow_id)
        if repository_id:
            filters.append(PullRequestModel.repository_id == repository_id)
        if state:
            filters.append(PullRequestModel.state == state)

        total = int(
            self._db.scalar(
                select(func.count()).select_from(PullRequestModel).where(*filters)
            )
            or 0
        )
        rows = list(
            self._db.scalars(
                select(PullRequestModel)
                .where(*filters)
                .order_by(PullRequestModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return [PullRequestRead.model_validate(row) for row in rows], total

    def get_pull_request(
        self,
        pull_request_id: str,
        *,
        refresh: bool = False,
    ) -> PullRequestStatusResponse:
        """Retrieve a persisted Pull Request and optionally refresh live CI/review status."""
        pr_row = self._db.scalar(
            select(PullRequestModel).where(PullRequestModel.id == pull_request_id)
        )
        if pr_row is None:
            raise ResourceNotFoundError(
                "Pull Request record not found.",
                details={"pull_request_id": pull_request_id},
            )

        if not refresh:
            return PullRequestStatusResponse(
                pull_request=PullRequestRead.model_validate(pr_row),
                live_refreshed=False,
            )

        repo = self._db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == pr_row.repository_id)
        )
        if repo is None:
            raise ResourceNotFoundError(
                "Repository linked to Pull Request not found.",
                details={"repository_id": pr_row.repository_id},
            )

        snapshot = self._github.get_pull_request_status(
            repo.full_name,
            pr_row.pr_number,
            installation_id=repo.github_installation_id,
        )
        pr_row.state = "merged" if snapshot.pull_request.merged else snapshot.pull_request.state
        pr_row.ci_status = snapshot.ci_status
        pr_row.review_decision = snapshot.review_decision
        if snapshot.pull_request.head_sha:
            pr_row.head_commit_sha = snapshot.pull_request.head_sha
        self._db.flush()

        return PullRequestStatusResponse(
            pull_request=PullRequestRead.model_validate(pr_row),
            live_refreshed=True,
            checks=snapshot.checks,
            reviews=snapshot.reviews,
        )

    def get_workflow_report(
        self,
        workflow_id: str,
        *,
        actor: str,
    ) -> WorkflowReportResponse:
        """Assemble a complete evidence-based workflow report from persisted records."""
        run = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        )
        if run is None:
            raise ResourceNotFoundError(
                "Workflow not found.",
                details={"workflow_id": workflow_id},
            )
        repo = self._db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == run.repository_id)
        )
        if repo is None:
            raise ResourceNotFoundError(
                "Repository linked to workflow not found.",
                details={"repository_id": run.repository_id},
            )

        latest_change_row = self._db.scalar(
            select(CodeChangeModel)
            .where(CodeChangeModel.workflow_id == run.id)
            .order_by(CodeChangeModel.revision_index.desc())
            .limit(1)
        )
        latest_change = (
            ReportCodeChangeSummary(
                revision_index=latest_change_row.revision_index,
                base_commit_sha=latest_change_row.base_commit_sha,
                patch_hash=latest_change_row.patch_hash,
                files_changed=list(latest_change_row.files_changed),
                lines_added=latest_change_row.lines_added,
                lines_removed=latest_change_row.lines_removed,
                total_changed_lines=latest_change_row.total_changed_lines,
                protected_path_flags=list(latest_change_row.protected_path_flags),
                scope_drift_files=list(latest_change_row.scope_drift_files),
                test_integrity_flags=list(latest_change_row.test_integrity_flags),
            )
            if latest_change_row is not None
            else None
        )

        test_rows = list(
            self._db.scalars(
                select(TestExecutionModel)
                .where(TestExecutionModel.workflow_id == run.id)
                .order_by(TestExecutionModel.run_index.asc())
            ).all()
        )
        test_summaries = [
            ReportTestExecutionSummary(
                run_index=row.run_index,
                sandbox_run_id=row.sandbox_run_id,
                passed=row.passed,
                install_exit_code=row.install_exit_code,
                test_exit_code=row.test_exit_code,
                duration_seconds=row.duration_seconds,
                timed_out=row.timed_out,
                network_policy=row.network_policy,
            )
            for row in test_rows
        ]

        finding_rows = list(
            self._db.scalars(
                select(StaticAnalysisFindingModel)
                .where(StaticAnalysisFindingModel.workflow_id == run.id)
                .order_by(
                    StaticAnalysisFindingModel.run_index.desc(),
                    StaticAnalysisFindingModel.finding_index.asc(),
                )
            ).all()
        )
        finding_summaries = [
            ReportStaticFindingSummary(
                run_index=row.run_index,
                tool=row.tool,
                rule_id=row.rule_id,
                severity=row.severity,
                file_path=row.file_path,
                start_line=row.start_line,
                end_line=row.end_line,
                message=row.message,
                confidence=row.confidence,
            )
            for row in finding_rows
        ]

        latest_review_row = self._db.scalar(
            select(CodeReviewModel)
            .where(CodeReviewModel.workflow_id == run.id)
            .order_by(CodeReviewModel.review_index.desc())
            .limit(1)
        )
        latest_review = (
            ReportCodeReviewSummary(
                review_index=latest_review_row.review_index,
                passed=latest_review_row.passed,
                summary=latest_review_row.summary,
                findings=list(latest_review_row.findings),
                test_integrity_assessments=list(latest_review_row.test_integrity_assessments),
                blocking_reasons=list(latest_review_row.blocking_reasons),
            )
            if latest_review_row is not None
            else None
        )

        approval_rows = list(
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
        usage_summary = WorkflowUsageSummaryRead(
            llm_calls_count=len(usage_rows),
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_tokens=total_tok,
            estimated_cost_usd=total_cost.quantize(Decimal("0.000001")),
            cost_status="estimated" if all_estimated else "unavailable",
            secrets_redacted_total=sum(r.secrets_redacted for r in usage_rows),
        )

        pr_row = self._db.scalar(
            select(PullRequestModel)
            .where(PullRequestModel.workflow_id == run.id)
            .order_by(PullRequestModel.created_at.desc())
            .limit(1)
        )

        artifact_rows = self._artifacts.list_workflow_artifacts(run.id)
        final_report_md: str | None = None
        final_report_artifacts = [
            row for row in artifact_rows if row.artifact_type == "final_report"
        ]
        if final_report_artifacts:
            latest_report_art: WorkflowArtifactModel = final_report_artifacts[-1]
            _meta, raw_bytes = self._artifacts.read_artifact_bytes(
                latest_report_art.id,
                actor=actor,
            )
            final_report_md = raw_bytes.decode("utf-8", errors="replace")

        return WorkflowReportResponse(
            workflow_id=run.id,
            repository_id=repo.id,
            repository_full_name=repo.full_name,
            title=run.title,
            status=run.status,
            current_node=run.current_node,
            base_branch=run.base_branch,
            base_commit_sha=run.base_commit_sha,
            created_by=run.created_by,
            created_at=run.created_at,
            updated_at=run.updated_at,
            latest_code_change=latest_change,
            test_executions=test_summaries,
            static_analysis_findings=finding_summaries,
            latest_code_review=latest_review,
            approvals=[WorkflowApprovalRead.model_validate(a) for a in approval_rows],
            usage_summary=usage_summary,
            pull_request=PullRequestRead.model_validate(pr_row) if pr_row is not None else None,
            artifacts=[ArtifactRead.model_validate(a) for a in artifact_rows],
            final_report_markdown=final_report_md,
        )