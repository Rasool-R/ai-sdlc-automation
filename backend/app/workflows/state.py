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
    approved_base_commit_sha: str | None = Field(default=None, min_length=7, max_length=64)
    approved_protected_paths: list[str] = Field(default_factory=list)
    clarification_answers: str | None = Field(default=None, max_length=4000)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_action_requirements(self) -> "HumanDecision":
        if self.action == "approve" and \
        (not self.approved_plan_hash or not self.approved_base_commit_sha):
            raise ValueError(
                "Plan approval must bind explicitly to both approved_plan_hash "
                "and approved_base_commit_sha."
            )
        elif self.action == "clarify" and \
        (not self.clarification_answers or not self.clarification_answers.strip()):
            raise ValueError(
                "clarification_answers must be non-empty when action is 'clarify'."
            )
        return self


class WorkflowState(TypedDict, total=False):
    """Typed, checkpoint-serializable LangGraph state for the vertical-slice workflow."""

    state_schema_version: str
    workflow_id: str
    thread_id: str
    repository_id: str
    repository_full_name: str
    is_private_repo: bool
    llm_data_sharing: bool
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

    requirement_spec: dict[str, Any] | None
    repo_analysis: dict[str, Any] | None
    technical_design: dict[str, Any] | None
    implementation_plan: dict[str, Any] | None
    plan_hash: str | None
    plan_approved: bool
    plan_approval_record: dict[str, Any] | None
    approved_protected_paths: list[str]

    coding_output: dict[str, Any] | None
    patch_validation: dict[str, Any] | None
    patch_apply_result: dict[str, Any] | None
    sandbox_report: dict[str, Any] | None

    usage_records: list[dict[str, Any]]
    node_history: list[str]
    error_code: str | None
    error_message: str | None


class WorkflowExecutionSnapshot(BaseModel):
    """Read-only summary returned after starting, resuming, or reconciling a workflow."""

    workflow_id: str
    thread_id: str
    state_schema_version: str
    status: str
    current_node: str | None = None
    base_commit_sha: str
    plan_hash: str | None = None
    interrupted: bool = False
    interrupt_payload: dict[str, Any] | None = None
    reused_existing: bool = False
    llm_calls_count: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: str = "0.000000"
    cost_status: str = "unavailable"
    sandbox_passed: bool | None = None
    patch_hash: str | None = None
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