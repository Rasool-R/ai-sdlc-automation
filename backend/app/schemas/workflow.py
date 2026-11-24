"""Pydantic v2 request and response contracts for workflow and task REST APIs."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowCreateRequest(BaseModel):
    """Request payload to enqueue a new version-pinned SDLC workflow run."""

    repository_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=3, max_length=255)
    request_text: str = Field(min_length=5, max_length=12000)
    base_branch: str | None = Field(default=None, min_length=1, max_length=200)
    base_commit_sha: str | None = Field(default=None, min_length=7, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=128)
    cost_limit_usd: Decimal = Field(
        gt=Decimal("0"),
        le=Decimal("10000"),
        description="Explicit positive cost cap in USD; workflows refuse to start without it.",
    )


class WorkflowApproveRequest(BaseModel):
    """Operator approval payload strictly bound to base_commit_sha and plan/patch hash."""

    approved_base_commit_sha: str = Field(min_length=7, max_length=64)
    approved_plan_hash: str | None = Field(default=None, min_length=64, max_length=64)
    approved_patch_hash: str | None = Field(default=None, min_length=64, max_length=64)
    approved_protected_paths: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_target_hash(self) -> "WorkflowApproveRequest":
        if not self.approved_plan_hash and not self.approved_patch_hash:
            raise ValueError(
                "Either approved_plan_hash (plan approval) or approved_patch_hash "
                "(commit/PR approval) is required."
            )
        return self


class WorkflowRejectRequest(BaseModel):
    """Operator rejection payload for a paused workflow."""

    reason: str | None = Field(default=None, max_length=1000)


class WorkflowResumeRequest(BaseModel):
    """Operator clarification payload to resume a workflow waiting for input."""

    clarification_answers: str = Field(min_length=1, max_length=4000)


class WorkflowCancelRequest(BaseModel):
    """Operator cancellation payload for an active or paused workflow."""

    reason: str | None = Field(default=None, max_length=1000)


class WorkflowApprovalRead(BaseModel):
    """Serialized immutable approval or rejection record."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_id: str
    approval_type: str
    decision: str
    actor: str
    base_commit_sha: str
    target_hash: str | None
    approved_protected_paths: list[str]
    reason: str | None
    decided_at: datetime


class WorkflowUsageSummaryRead(BaseModel):
    """Aggregated token and cost metrics recorded for a workflow."""

    llm_calls_count: int = Field(default=0, ge=0)
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal("0.000000"))
    cost_status: Literal["estimated", "unavailable"] = "unavailable"
    secrets_redacted_total: int = Field(default=0, ge=0)


class WorkflowRead(BaseModel):
    """Serialized workflow run projection."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    repository_id: str
    base_branch: str
    base_commit_sha: str
    thread_id: str
    idempotency_key: str
    title: str
    request_text: str
    status: str
    current_node: str | None
    cost_limit_usd: Decimal
    created_by: str
    created_at: datetime
    updated_at: datetime


class WorkflowDetailResponse(WorkflowRead):
    """Detailed workflow view combining projection, approvals, step count, and cost summary."""

    steps_recorded: int = Field(default=0, ge=0)
    tasks_count: int = Field(default=0, ge=0)
    approvals: list[WorkflowApprovalRead] = Field(default_factory=list)
    usage_summary: WorkflowUsageSummaryRead = Field(default_factory=WorkflowUsageSummaryRead)


class WorkflowListResponse(BaseModel):
    """Paginated list of workflow runs."""

    items: list[WorkflowRead]
    total: int = Field(ge=0)


class WorkflowActionResponse(BaseModel):
    """Response confirming an asynchronous workflow dispatch or state transition."""

    workflow_id: str
    status: str
    action: str
    job_id: str | None = None
    reused_existing: bool = False


class WorkflowTaskRead(BaseModel):
    """Serialized persisted workflow task including resolved dependency keys and IDs."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_id: str
    task_key: str
    title: str
    description: str
    priority: str
    status: str
    expected_files: list[str]
    acceptance_criteria: list[str]
    verification_steps: list[str]
    risk_notes: str
    depends_on_task_ids: list[str] = Field(default_factory=list)
    depends_on_task_keys: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class WorkflowTaskListResponse(BaseModel):
    """List of tasks belonging to a workflow run."""

    workflow_id: str
    items: list[WorkflowTaskRead]
    total: int = Field(ge=0)