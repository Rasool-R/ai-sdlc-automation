"""Versioned LangGraph workflow state schema, human decisions, and approval hash binding."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

from app.agents.schemas import ImplementationPlan, TechnicalDesign
from app.core.exceptions import ValidationFailedError

WORKFLOW_STATE_SCHEMA_VERSION = "1.0"

WorkflowStatus = Literal[
    "pending",
    "running",
    "waiting_clarification",
    "waiting_plan_approval",
    "waiting_commit_approval",
    "completed",
    "rejected",
    "failed",
    "cancelled",
]


class HumanDecision(BaseModel):
    """Validated operator decision supplied when resuming a paused LangGraph interrupt."""

    action: Literal["approve", "reject", "clarify"]
    actor: str = Field(min_length=1, max_length=100)
    approved_plan_hash: str | None = Field(default=None, min_length=64, max_length=64)
    approved_patch_hash: str | None = Field(default=None, min_length=64, max_length=64)
    approved_base_commit_sha: str | None = Field(default=None, min_length=7, max_length=64)
    approved_protected_paths: list[str] = Field(default_factory=list)
    clarification_answers: str | None = Field(default=None, max_length=4000)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_action_requirements(self) -> "HumanDecision":
        if self.action == "approve":
            if not self.approved_base_commit_sha:
                raise ValueError(
                    "Approval must bind explicitly to approved_base_commit_sha."
                )
            if not self.approved_plan_hash and not self.approved_patch_hash:
                raise ValueError(
                    "Approval must bind explicitly to either approved_plan_hash "
                    "or approved_patch_hash."
                )
        elif self.action == "clarify":
            if not self.clarification_answers or not self.clarification_answers.strip():
                raise ValueError(
                    "clarification_answers must be non-empty when action is 'clarify'."
                )
        return self


class WorkflowState(TypedDict, total=False):
    """Typed, checkpoint-serializable LangGraph state for the SDLC workflow."""

    state_schema_version: str
    workflow_id: str
    thread_id: str
    repository_id: str
    repository_full_name: str
    is_private_repo: bool
    llm_data_sharing: bool
    tracing_enabled: bool
    base_branch: str
    base_commit_sha: str
    workspace_dir: str
    title: str
    request_text: str
    cost_limit_usd: str

    status: str
    current_node: str
    started_at_iso: str
    llm_calls_count: int
    total_tokens: int
    estimated_cost_usd: str
    cost_status: str
    clarification_attempts: int
    clarification_answers: str | None

    test_correction_loops: int
    review_correction_loops: int
    total_correction_loops: int
    pending_correction_feedback: str | None

    requirement_spec: dict[str, Any] | None
    repo_analysis: dict[str, Any] | None
    technical_design: dict[str, Any] | None
    implementation_plan: dict[str, Any] | None
    plan_hash: str | None
    plan_approved: bool
    plan_approval_record: dict[str, Any] | None
    approved_protected_paths: list[str]

    touched_files: list[str]
    coding_output: dict[str, Any] | None
    patch_validation: dict[str, Any] | None
    patch_apply_result: dict[str, Any] | None
    patch_hash: str | None
    sandbox_report: dict[str, Any] | None
    static_analysis_report: dict[str, Any] | None
    static_analysis_summary: str | None
    last_test_diagnosis: dict[str, Any] | None
    code_review_report: dict[str, Any] | None

    code_change_history: list[dict[str, Any]]
    test_execution_history: list[dict[str, Any]]
    static_analysis_history: list[dict[str, Any]]
    code_review_history: list[dict[str, Any]]

    commit_approved: bool
    commit_approval_record: dict[str, Any] | None
    release_output: dict[str, Any] | None

    cancelled_by: str | None
    cancel_reason: str | None
    usage_records: list[dict[str, Any]]
    node_history: list[str]
    error_code: str | None
    error_message: str | None


class WorkflowExecutionSnapshot(BaseModel):
    """Read-only summary returned after starting, resuming, cancelling, or reconciling."""

    workflow_id: str
    thread_id: str
    state_schema_version: str
    status: str
    current_node: str | None = None
    base_commit_sha: str
    plan_hash: str | None = None
    patch_hash: str | None = None
    interrupted: bool = False
    interrupt_payload: dict[str, Any] | None = None
    reused_existing: bool = False
    tracing_enabled: bool = False
    llm_calls_count: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: str = "0.000000"
    cost_status: str = "unavailable"
    test_correction_loops: int = Field(default=0, ge=0)
    review_correction_loops: int = Field(default=0, ge=0)
    total_correction_loops: int = Field(default=0, ge=0)
    sandbox_passed: bool | None = None
    static_analysis_passed: bool | None = None
    static_analysis_blocking_count: int = Field(default=0, ge=0)
    review_passed: bool | None = None
    commit_approved: bool = False
    pr_title: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    node_history: list[str] = Field(default_factory=list)


def compute_plan_hash(
    *,
    base_commit_sha: str,
    design: TechnicalDesign,
    plan: ImplementationPlan,
) -> str:
    """Compute a deterministic SHA-256 hash binding approval to exact design, plan, and SHA."""
    canonical = json.dumps(
        {
            "base_commit_sha": base_commit_sha,
            "design": design.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_state_schema_version(state: Mapping[str, Any]) -> None:
    """Verify checkpoint state carries the compatible schema version before resuming."""
    found_version = state.get("state_schema_version")
    if found_version != WORKFLOW_STATE_SCHEMA_VERSION:
        raise ValidationFailedError(
            "Cannot resume workflow checkpoint with an incompatible state schema version.",
            details={
                "expected_schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
                "found_schema_version": str(found_version),
            },
        )