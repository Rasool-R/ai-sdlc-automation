"""Pydantic v2 contracts for Pull Requests, artifacts, and evidence-backed workflow reports."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.github.pr_schemas import GitHubCheckRunSummary, GitHubReviewSummary
from app.schemas.workflow import WorkflowApprovalRead, WorkflowUsageSummaryRead


class PullRequestDeliveryResult(BaseModel):
    """Outcome of an idempotent branch push and Pull Request creation."""

    pull_request_id: str
    pr_number: int = Field(ge=1)
    title: str
    html_url: str
    head_branch: str
    head_commit_sha: str
    base_branch: str
    base_commit_sha: str
    patch_hash: str
    operation_id: str
    reused_existing: bool = False


class PullRequestRead(BaseModel):
    """Serialized persisted Pull Request record."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_id: str
    repository_id: str
    operation_id: str
    provider: str
    pr_number: int
    title: str
    body: str
    head_branch: str
    base_branch: str
    base_commit_sha: str
    head_commit_sha: str
    patch_hash: str
    html_url: str
    state: str
    ci_status: str
    review_decision: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class PullRequestListResponse(BaseModel):
    """Paginated list of persisted Pull Requests."""

    items: list[PullRequestRead]
    total: int = Field(ge=0)


class PullRequestStatusResponse(BaseModel):
    """Pull Request detail combined with optional live on-demand GitHub CI/review checks."""

    pull_request: PullRequestRead
    live_refreshed: bool = False
    checks: list[GitHubCheckRunSummary] = Field(default_factory=list)
    reviews: list[GitHubReviewSummary] = Field(default_factory=list)


class ArtifactRead(BaseModel):
    """Serialized metadata for an externally stored, checksummed workflow artifact."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_id: str
    artifact_type: str
    name: str
    version: int
    content_type: str
    storage_backend: str
    checksum_sha256: str
    size_bytes: int
    created_by: str
    retention_days: int
    expires_at: datetime
    created_at: datetime


class ArtifactListResponse(BaseModel):
    """List of versioned artifacts belonging to a workflow run."""

    workflow_id: str
    items: list[ArtifactRead]
    total: int = Field(ge=0)


class ReportCodeChangeSummary(BaseModel):
    """Latest validated cumulative code change summary for the workflow report."""

    revision_index: int
    base_commit_sha: str
    patch_hash: str
    files_changed: list[str]
    lines_added: int
    lines_removed: int
    total_changed_lines: int
    protected_path_flags: list[dict[str, Any]]
    scope_drift_files: list[str]
    test_integrity_flags: list[dict[str, Any]]


class ReportTestExecutionSummary(BaseModel):
    """Persisted isolated sandbox test execution entry."""

    run_index: int
    sandbox_run_id: str
    passed: bool
    install_exit_code: int | None
    test_exit_code: int | None
    duration_seconds: float
    timed_out: bool
    network_policy: str


class ReportStaticFindingSummary(BaseModel):
    """Persisted normalized static-analysis finding."""

    run_index: int
    tool: str
    rule_id: str
    severity: str
    file_path: str
    start_line: int | None
    end_line: int | None
    message: str
    confidence: str


class ReportCodeReviewSummary(BaseModel):
    """Latest code review disposition included in the workflow report."""

    review_index: int
    passed: bool
    summary: str
    findings: list[dict[str, Any]]
    test_integrity_assessments: list[dict[str, Any]]
    blocking_reasons: list[str]


class WorkflowReportResponse(BaseModel):
    """Evidence-based workflow completion and audit report assembled from persisted records."""

    workflow_id: str
    repository_id: str
    repository_full_name: str
    title: str
    status: str
    current_node: str | None
    base_branch: str
    base_commit_sha: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    latest_code_change: ReportCodeChangeSummary | None = None
    test_executions: list[ReportTestExecutionSummary] = Field(default_factory=list)
    static_analysis_findings: list[ReportStaticFindingSummary] = Field(default_factory=list)
    latest_code_review: ReportCodeReviewSummary | None = None
    approvals: list[WorkflowApprovalRead] = Field(default_factory=list)
    usage_summary: WorkflowUsageSummaryRead = Field(default_factory=WorkflowUsageSummaryRead)
    pull_request: PullRequestRead | None = None
    artifacts: list[ArtifactRead] = Field(default_factory=list)
    final_report_markdown: str | None = None