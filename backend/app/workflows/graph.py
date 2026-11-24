"""Deterministic LangGraph StateGraph orchestrator for the Phase 4 vertical slice.

Architecture invariants:
- LangGraph checkpoints are the single source of truth for execution state.
- WorkflowRunModel is an application-level projection synced idempotently at transitions.
- Routing is strictly deterministic Python (no LLM-based Supervisor).
- Human approval interrupts checkpoint state and bind to (plan_hash, base_commit_sha).
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.coding_agent import CodingAgent
from app.agents.planning_agent import PlanningAgent
from app.agents.repository_analysis_agent import RepositoryAnalysisAgent
from app.agents.requirement_agent import RequirementAnalysisAgent
from app.agents.schemas import (
    ImplementationPlan,
    RepositoryAnalysisReport,
    RequirementSpecification,
    TechnicalDesign,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from app.execution.patch_service import PatchService
from app.execution.sandbox_client import SandboxClient
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.intelligence.retrieval import HybridRetrievalService
from app.models.repository import RepositoryModel
from app.models.workflow import WorkflowRunModel
from app.services.audit_service import AuditService
from app.workflows.state import (
    WORKFLOW_STATE_SCHEMA_VERSION,
    HumanDecision,
    WorkflowExecutionSnapshot,
    WorkflowState,
    compute_plan_hash,
    validate_state_schema_version,
)

logger = logging.getLogger(__name__)

MAX_CLARIFICATION_ATTEMPTS = 2


def _append_history(state: WorkflowState, node_name: str) -> list[str]:
    return [*state.get("node_history", []), node_name]


def _accumulate_usage(state: WorkflowState, usage: LLMUsageMetrics) -> dict[str, Any]:
    calls = int(state.get("llm_calls_count", 0)) + 1
    tokens = int(state.get("total_tokens", 0)) + int(usage.total_tokens or 0)
    current_cost = Decimal(state.get("estimated_cost_usd", "0.000000"))
    prev_status = state.get("cost_status", "estimated")

    if usage.estimated_cost_usd is not None and usage.cost_status == "estimated":
        new_cost = (current_cost + usage.estimated_cost_usd).quantize(Decimal("0.000001"))
        cost_status = "unavailable" if prev_status == "unavailable" and calls > 1 else "estimated"
    else:
        new_cost = current_cost
        cost_status = "unavailable"

    records = [*state.get("usage_records", []), usage.model_dump(mode="json")]
    return {
        "llm_calls_count": calls,
        "total_tokens": tokens,
        "estimated_cost_usd": str(new_cost),
        "cost_status": cost_status,
        "usage_records": records,
    }


def _check_budget_limits(state: WorkflowState, settings: Settings) -> tuple[str, str] | None:
    """Enforce configured call, cost, and wall-clock limits before invoking an LLM."""
    calls = int(state.get("llm_calls_count", 0))
    if calls >= settings.MAX_LLM_CALLS_PER_WORKFLOW:
        return (
            "BUDGET_EXHAUSTED",
            f"Workflow reached maximum LLM calls limit ({settings.MAX_LLM_CALLS_PER_WORKFLOW}).",
        )

    cost_limit = Decimal(state.get("cost_limit_usd", "0"))
    current_cost = Decimal(state.get("estimated_cost_usd", "0"))
    if cost_limit <= Decimal("0") or current_cost >= cost_limit:
        return (
            "BUDGET_EXHAUSTED",
            f"Workflow estimated cost ({current_cost} USD) reached limit ({cost_limit} USD).",
        )

    started_iso = state.get("started_at_iso")
    if started_iso:
        try:
            started_dt = datetime.fromisoformat(started_iso)
            elapsed = (datetime.now(UTC) - started_dt).total_seconds()
            if elapsed > settings.WORKFLOW_TIMEOUT_SECONDS:
                return (
                    "WORKFLOW_TIMEOUT",
                    f"Workflow exceeded wall-clock limit of {settings.WORKFLOW_TIMEOUT_SECONDS}s.",
                )
        except ValueError:
            pass

    return None


def _extract_interrupt_payload(snapshot: Any) -> dict[str, Any] | None:
    """Inspect a LangGraph StateSnapshot for an active human interrupt payload."""
    tasks = getattr(snapshot, "tasks", ()) or ()
    for task in tasks:
        interrupts = getattr(task, "interrupts", ()) or ()
        for intr in interrupts:
            value = getattr(intr, "value", None)
            if isinstance(value, dict):
                return dict(value)
    return None


def _derive_status_from_snapshot(
    snapshot: Any,
) -> tuple[str, str | None, bool, dict[str, Any] | None]:
    """Derive authoritative projection status and node from a LangGraph checkpoint snapshot."""
    values: dict[str, Any] = dict(getattr(snapshot, "values", {}) or {})
    pending_next = tuple(getattr(snapshot, "next", ()) or ())
    interrupt_payload = _extract_interrupt_payload(snapshot)
    is_interrupted = bool(pending_next)

    if is_interrupted:
        intr_type = interrupt_payload.get("type") if interrupt_payload else None
        if intr_type == "clarification_required":
            return "waiting_clarification", "human_clarification", True, interrupt_payload
        if intr_type == "plan_approval_required":
            return "waiting_plan_approval", "human_plan_approval", True, interrupt_payload
        next_node = str(pending_next[0]) if pending_next else values.get("current_node")
        return "running", next_node, True, interrupt_payload

    status = str(values.get("status", "failed"))
    current_node = values.get("current_node")
    return status, str(current_node) if current_node is not None else None, False, None


def build_vertical_slice_graph(
    *,
    db: Session,
    llm_client: LLMClient,
    retrieval_service: HybridRetrievalService,
    patch_service: PatchService,
    sandbox_client: SandboxClient,
    checkpointer: Any,
    settings: Settings | None = None,
) -> Any:
    """Compile the deterministic Phase 4 vertical-slice LangGraph StateGraph."""
    resolved_settings = settings or get_settings()
    req_agent = RequirementAnalysisAgent(llm_client)
    repo_agent = RepositoryAnalysisAgent(llm_client, retrieval_service)
    plan_agent = PlanningAgent(llm_client)
    coding_agent = CodingAgent(llm_client, patch_service)

    def requirement_analysis_node(state: WorkflowState) -> dict[str, Any]:
        budget_violation = _check_budget_limits(state, resolved_settings)
        if budget_violation is not None:
            return {
                "status": "failed",
                "current_node": "requirement_analysis",
                "error_code": budget_violation[0],
                "error_message": budget_violation[1],
                "node_history": _append_history(state, "requirement_analysis"),
            }

        try:
            result = req_agent.run(
                title=state["title"],
                request_text=state["request_text"],
                repository_full_name=state["repository_full_name"],
                is_private_repo=state["is_private_repo"],
                llm_data_sharing=state["llm_data_sharing"],
                clarification_answers=state.get("clarification_answers"),
            )
        except AppError as exc:
            return {
                "status": "failed",
                "current_node": "requirement_analysis",
                "error_code": exc.code,
                "error_message": exc.message,
                "node_history": _append_history(state, "requirement_analysis"),
            }

        usage_update = _accumulate_usage(state, result.usage)
        return {
            **usage_update,
            "status": "running",
            "current_node": "requirement_analysis",
            "requirement_spec": result.specification.model_dump(mode="json"),
            "node_history": _append_history(state, "requirement_analysis"),
        }

    def route_after_requirement(state: WorkflowState) -> str:
        if state.get("status") == "failed":
            return "end"
        raw_spec = state.get("requirement_spec")
        if not raw_spec:
            return "end"
        if bool(raw_spec.get("needs_clarification")):
            attempts = int(state.get("clarification_attempts", 0))
            if attempts >= MAX_CLARIFICATION_ATTEMPTS:
                return "fail_clarification_limit"
            return "human_clarification"
        return "repository_analysis"

    def fail_clarification_limit_node(state: WorkflowState) -> dict[str, Any]:
        return {
            "status": "failed",
            "current_node": "fail_clarification_limit",
            "error_code": "CLARIFICATION_LIMIT_EXCEEDED",
            "error_message": (
                f"Requirement still ambiguous after {MAX_CLARIFICATION_ATTEMPTS} "
                "clarification attempt(s)."
            ),
            "node_history": _append_history(state, "fail_clarification_limit"),
        }

    def human_clarification_node(state: WorkflowState) -> dict[str, Any]:
        raw_spec = state.get("requirement_spec") or {}
        decision_raw = interrupt(
            {
                "type": "clarification_required",
                "workflow_id": state["workflow_id"],
                "base_commit_sha": state["base_commit_sha"],
                "questions": list(raw_spec.get("clarification_questions", [])),
                "ambiguities": list(raw_spec.get("ambiguities", [])),
            }
        )
        try:
            decision = HumanDecision.model_validate(decision_raw)
        except ValidationError as exc:
            return {
                "status": "failed",
                "current_node": "human_clarification",
                "error_code": "INVALID_HUMAN_DECISION",
                "error_message": f"Invalid clarification payload: {exc.error_count()} error(s).",
                "node_history": _append_history(state, "human_clarification"),
            }

        if decision.action == "reject":
            return {
                "status": "rejected",
                "current_node": "human_clarification",
                "error_code": "OPERATOR_REJECTED",
                "error_message": decision.reason or "Rejected by operator during clarification.",
                "node_history": _append_history(state, "human_clarification"),
            }

        if decision.action != "clarify":
            return {
                "status": "failed",
                "current_node": "human_clarification",
                "error_code": "UNEXPECTED_HUMAN_ACTION",
                "error_message": f"Expected 'clarify' or 'reject', got '{decision.action}'.",
                "node_history": _append_history(state, "human_clarification"),
            }

        existing_answers = state.get("clarification_answers") or ""
        combined_answers = (
            f"{existing_answers}\n{decision.clarification_answers}".strip()
            if existing_answers
            else (decision.clarification_answers or "")
        )
        return {
            "status": "running",
            "current_node": "human_clarification",
            "clarification_answers": combined_answers,
            "clarification_attempts": int(state.get("clarification_attempts", 0)) + 1,
            "node_history": _append_history(state, "human_clarification"),
        }

    def route_after_clarification(state: WorkflowState) -> str:
        if state.get("status") in {"failed", "rejected"}:
            return "end"
        return "requirement_analysis"

    def repository_analysis_node(state: WorkflowState) -> dict[str, Any]:
        budget_violation = _check_budget_limits(state, resolved_settings)
        if budget_violation is not None:
            return {
                "status": "failed",
                "current_node": "repository_analysis",
                "error_code": budget_violation[0],
                "error_message": budget_violation[1],
                "node_history": _append_history(state, "repository_analysis"),
            }

        repo = db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == state["repository_id"])
        )
        if repo is None:
            return {
                "status": "failed",
                "current_node": "repository_analysis",
                "error_code": "RESOURCE_NOT_FOUND",
                "error_message": "Connected repository record not found.",
                "node_history": _append_history(state, "repository_analysis"),
            }

        try:
            requirement = RequirementSpecification.model_validate(state["requirement_spec"])
            result = repo_agent.run(
                repository=repo,
                base_commit_sha=state["base_commit_sha"],
                requirement=requirement,
                workspace_dir=Path(state["workspace_dir"]),
            )
        except (AppError, ValidationError) as exc:
            err_code = exc.code if isinstance(exc, AppError) else "STATE_VALIDATION_ERROR"
            err_msg = exc.message if isinstance(exc, AppError) else str(exc)
            return {
                "status": "failed",
                "current_node": "repository_analysis",
                "error_code": err_code,
                "error_message": err_msg,
                "node_history": _append_history(state, "repository_analysis"),
            }

        usage_update = _accumulate_usage(state, result.usage)
        return {
            **usage_update,
            "status": "running",
            "current_node": "repository_analysis",
            "repo_analysis": result.report.model_dump(mode="json"),
            "node_history": _append_history(state, "repository_analysis"),
        }

    def route_if_running(next_node: str) -> Any:
        def _router(state: WorkflowState) -> str:
            if state.get("status") in {"failed", "rejected", "cancelled"}:
                return "end"
            return next_node

        return _router

    def planning_node(state: WorkflowState) -> dict[str, Any]:
        budget_violation = _check_budget_limits(state, resolved_settings)
        if budget_violation is not None:
            return {
                "status": "failed",
                "current_node": "planning",
                "error_code": budget_violation[0],
                "error_message": budget_violation[1],
                "node_history": _append_history(state, "planning"),
            }

        try:
            requirement = RequirementSpecification.model_validate(state["requirement_spec"])
            analysis = RepositoryAnalysisReport.model_validate(state["repo_analysis"])
            result = plan_agent.run(
                requirement=requirement,
                repo_analysis=analysis,
                is_private_repo=state["is_private_repo"],
                llm_data_sharing=state["llm_data_sharing"],
            )
        except (AppError, ValidationError) as exc:
            err_code = exc.code if isinstance(exc, AppError) else "STATE_VALIDATION_ERROR"
            err_msg = exc.message if isinstance(exc, AppError) else str(exc)
            return {
                "status": "failed",
                "current_node": "planning",
                "error_code": err_code,
                "error_message": err_msg,
                "node_history": _append_history(state, "planning"),
            }

        plan_hash = compute_plan_hash(
            base_commit_sha=state["base_commit_sha"],
            design=result.design,
            plan=result.plan,
        )
        usage_update = _accumulate_usage(state, result.usage)
        return {
            **usage_update,
            "status": "running",
            "current_node": "planning",
            "technical_design": result.design.model_dump(mode="json"),
            "implementation_plan": result.plan.model_dump(mode="json"),
            "plan_hash": plan_hash,
            "plan_approved": False,
            "node_history": _append_history(state, "planning"),
        }

    def human_plan_approval_node(state: WorkflowState) -> dict[str, Any]:
        plan_dict = state.get("implementation_plan") or {}
        decision_raw = interrupt(
            {
                "type": "plan_approval_required",
                "workflow_id": state["workflow_id"],
                "base_commit_sha": state["base_commit_sha"],
                "plan_hash": state["plan_hash"],
                "technical_design": state.get("technical_design"),
                "implementation_plan": plan_dict,
                "expected_files": list(plan_dict.get("expected_files", [])),
                "declared_protected_paths": list(plan_dict.get("declared_protected_paths", [])),
                "estimated_cost_usd": state.get("estimated_cost_usd", "0.000000"),
                "cost_status": state.get("cost_status", "unavailable"),
            }
        )

        try:
            decision = HumanDecision.model_validate(decision_raw)
        except ValidationError as exc:
            return {
                "status": "failed",
                "current_node": "human_plan_approval",
                "error_code": "INVALID_HUMAN_DECISION",
                "error_message": f"Invalid plan approval payload: {exc.error_count()} error(s).",
                "node_history": _append_history(state, "human_plan_approval"),
            }

        if decision.action == "reject":
            return {
                "status": "rejected",
                "current_node": "human_plan_approval",
                "plan_approved": False,
                "plan_approval_record": {
                    "action": "reject",
                    "actor": decision.actor,
                    "reason": decision.reason,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "plan_hash": state.get("plan_hash"),
                    "base_commit_sha": state["base_commit_sha"],
                },
                "node_history": _append_history(state, "human_plan_approval"),
            }

        if decision.action != "approve":
            return {
                "status": "failed",
                "current_node": "human_plan_approval",
                "error_code": "UNEXPECTED_HUMAN_ACTION",
                "error_message": f"Expected 'approve' or 'reject', got '{decision.action}'.",
                "node_history": _append_history(state, "human_plan_approval"),
            }

        # Safety Invariant: approval must bind strictly to exact plan_hash and base_commit_sha
        if (
            decision.approved_plan_hash != state.get("plan_hash")
            or decision.approved_base_commit_sha != state["base_commit_sha"]
        ):
            return {
                "status": "failed",
                "current_node": "human_plan_approval",
                "plan_approved": False,
                "error_code": "APPROVAL_BINDING_MISMATCH",
                "error_message": (
                    "Plan approval hash or base_commit_sha does not match current workflow state."
                ),
                "node_history": _append_history(state, "human_plan_approval"),
            }

        return {
            "status": "running",
            "current_node": "human_plan_approval",
            "plan_approved": True,
            "approved_protected_paths": list(decision.approved_protected_paths),
            "plan_approval_record": {
                "action": "approve",
                "actor": decision.actor,
                "recorded_at": datetime.now(UTC).isoformat(),
                "plan_hash": state["plan_hash"],
                "base_commit_sha": state["base_commit_sha"],
                "approved_protected_paths": list(decision.approved_protected_paths),
            },
            "node_history": _append_history(state, "human_plan_approval"),
        }

    def route_after_plan_approval(state: WorkflowState) -> str:
        if state.get("plan_approved") is True and state.get("status") == "running":
            return "coding"
        return "end"

    def coding_node(state: WorkflowState) -> dict[str, Any]:
        if state.get("plan_approved") is not True:
            return {
                "status": "failed",
                "current_node": "coding",
                "error_code": "PLAN_APPROVAL_REQUIRED",
                "error_message": "Coding cannot execute without recorded human plan approval.",
                "node_history": _append_history(state, "coding"),
            }

        budget_violation = _check_budget_limits(state, resolved_settings)
        if budget_violation is not None:
            return {
                "status": "failed",
                "current_node": "coding",
                "error_code": budget_violation[0],
                "error_message": budget_violation[1],
                "node_history": _append_history(state, "coding"),
            }

        try:
            design = TechnicalDesign.model_validate(state["technical_design"])
            plan = ImplementationPlan.model_validate(state["implementation_plan"])
            approved_protected = list(state.get("approved_protected_paths", []))
            workspace_path = Path(state["workspace_dir"])

            coding_res = coding_agent.run(
                workspace_dir=workspace_path,
                base_commit_sha=state["base_commit_sha"],
                design=design,
                plan=plan,
                is_private_repo=state["is_private_repo"],
                llm_data_sharing=state["llm_data_sharing"],
                approved_protected_paths=approved_protected,
            )
        except (AppError, ValidationError) as exc:
            err_code = exc.code if isinstance(exc, AppError) else "STATE_VALIDATION_ERROR"
            err_msg = exc.message if isinstance(exc, AppError) else str(exc)
            return {
                "status": "failed",
                "current_node": "coding",
                "error_code": err_code,
                "error_message": err_msg,
                "node_history": _append_history(state, "coding"),
            }

        usage_update = _accumulate_usage(state, coding_res.usage)

        if not coding_res.validation.valid:
            return {
                **usage_update,
                "status": "failed",
                "current_node": "coding",
                "coding_output": coding_res.output.model_dump(mode="json"),
                "patch_validation": coding_res.validation.model_dump(mode="json"),
                "error_code": "PATCH_VALIDATION_FAILED",
                "error_message": "; ".join(coding_res.validation.errors)
                or "Patch failed deterministic backend validation.",
                "node_history": _append_history(state, "coding"),
            }

        try:
            applied = patch_service.apply_patch(
                workspace_dir=workspace_path,
                patch=coding_res.output.patch,
                expected_files=plan.expected_files,
                declared_protected_paths=plan.declared_protected_paths,
                approved_protected_paths=approved_protected,
                plan_declares_dependency_change=plan.declares_dependency_change,
            )
        except AppError as exc:
            return {
                **usage_update,
                "status": "failed",
                "current_node": "coding",
                "coding_output": coding_res.output.model_dump(mode="json"),
                "patch_validation": coding_res.validation.model_dump(mode="json"),
                "error_code": exc.code,
                "error_message": exc.message,
                "node_history": _append_history(state, "coding"),
            }

        return {
            **usage_update,
            "status": "running",
            "current_node": "coding",
            "coding_output": coding_res.output.model_dump(mode="json"),
            "patch_validation": coding_res.validation.model_dump(mode="json"),
            "patch_apply_result": applied.model_dump(mode="json"),
            "node_history": _append_history(state, "coding"),
        }

    def sandbox_test_node(state: WorkflowState) -> dict[str, Any]:
        try:
            report = sandbox_client.run_isolated_tests(
                workflow_id=state["workflow_id"],
                repository_id=state["repository_id"],
                base_commit_sha=state["base_commit_sha"],
                workspace_dir=Path(state["workspace_dir"]),
            )
        except AppError as exc:
            return {
                "status": "failed",
                "current_node": "sandbox_test",
                "error_code": exc.code,
                "error_message": exc.message,
                "node_history": _append_history(state, "sandbox_test"),
            }

        if not report.passed:
            return {
                "status": "failed",
                "current_node": "sandbox_test",
                "sandbox_report": report.model_dump(mode="json"),
                "error_code": "SANDBOX_TESTS_FAILED",
                "error_message": "Isolated sandbox test execution failed.",
                "node_history": _append_history(state, "sandbox_test"),
            }

        return {
            "status": "completed",
            "current_node": "sandbox_test",
            "sandbox_report": report.model_dump(mode="json"),
            "node_history": _append_history(state, "sandbox_test"),
        }

    graph_builder = StateGraph(WorkflowState)
    graph_builder.add_node("requirement_analysis", requirement_analysis_node)
    graph_builder.add_node("fail_clarification_limit", fail_clarification_limit_node)
    graph_builder.add_node("human_clarification", human_clarification_node)
    graph_builder.add_node("repository_analysis", repository_analysis_node)
    graph_builder.add_node("planning", planning_node)
    graph_builder.add_node("human_plan_approval", human_plan_approval_node)
    graph_builder.add_node("coding", coding_node)
    graph_builder.add_node("sandbox_test", sandbox_test_node)

    graph_builder.add_edge(START, "requirement_analysis")
    graph_builder.add_conditional_edges(
        "requirement_analysis",
        route_after_requirement,
        {
            "human_clarification": "human_clarification",
            "fail_clarification_limit": "fail_clarification_limit",
            "repository_analysis": "repository_analysis",
            "end": END,
        },
    )
    graph_builder.add_edge("fail_clarification_limit", END)
    graph_builder.add_conditional_edges(
        "human_clarification",
        route_after_clarification,
        {
            "requirement_analysis": "requirement_analysis",
            "end": END,
        },
    )
    graph_builder.add_conditional_edges(
        "repository_analysis",
        route_if_running("planning"),
        {"planning": "planning", "end": END},
    )
    graph_builder.add_conditional_edges(
        "planning",
        route_if_running("human_plan_approval"),
        {"human_plan_approval": "human_plan_approval", "end": END},
    )
    graph_builder.add_conditional_edges(
        "human_plan_approval",
        route_after_plan_approval,
        {"coding": "coding", "end": END},
    )
    graph_builder.add_conditional_edges(
        "coding",
        route_if_running("sandbox_test"),
        {"sandbox_test": "sandbox_test", "end": END},
    )
    graph_builder.add_edge("sandbox_test", END)

    return graph_builder.compile(checkpointer=checkpointer)


class VerticalSliceOrchestrator:
    """Runs and resumes vertical-slice workflows while keeping WorkflowRunModel in sync."""

    def __init__(
        self,
        db: Session,
        *,
        llm_client: LLMClient,
        retrieval_service: HybridRetrievalService,
        patch_service: PatchService,
        sandbox_client: SandboxClient,
        checkpointer: Any,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._settings = settings or get_settings()
        self._audit = AuditService(db)
        self._graph = build_vertical_slice_graph(
            db=db,
            llm_client=llm_client,
            retrieval_service=retrieval_service,
            patch_service=patch_service,
            sandbox_client=sandbox_client,
            checkpointer=checkpointer,
            settings=self._settings,
        )

    def _build_snapshot_response(
        self,
        *,
        run: WorkflowRunModel,
        checkpoint_snapshot: Any,
        reused_existing: bool = False,
    ) -> WorkflowExecutionSnapshot:
        values: dict[str, Any] = dict(getattr(checkpoint_snapshot, "values", {}) or {})
        status, current_node, interrupted, interrupt_payload = _derive_status_from_snapshot(
            checkpoint_snapshot
        )
        sandbox_raw = values.get("sandbox_report")
        sandbox_passed = (
            bool(sandbox_raw.get("passed")) if isinstance(sandbox_raw, dict) else None
        )
        patch_apply_raw = values.get("patch_apply_result")
        patch_hash = (
            str(patch_apply_raw.get("patch_hash"))
            if isinstance(patch_apply_raw, dict) and patch_apply_raw.get("patch_hash")
            else None
        )
        return WorkflowExecutionSnapshot(
            workflow_id=run.id,
            thread_id=run.thread_id,
            state_schema_version=str(
                values.get("state_schema_version", WORKFLOW_STATE_SCHEMA_VERSION)
            ),
            status=status,
            current_node=current_node,
            base_commit_sha=run.base_commit_sha,
            plan_hash=str(values["plan_hash"]) if values.get("plan_hash") else None,
            interrupted=interrupted,
            interrupt_payload=interrupt_payload,
            reused_existing=reused_existing,
            llm_calls_count=int(values.get("llm_calls_count", 0)),
            total_tokens=int(values.get("total_tokens", 0)),
            estimated_cost_usd=str(values.get("estimated_cost_usd", "0.000000")),
            cost_status=str(values.get("cost_status", "unavailable")),
            sandbox_passed=sandbox_passed,
            patch_hash=patch_hash,
            error_code=str(values["error_code"]) if values.get("error_code") else None,
            error_message=str(values["error_message"]) if values.get("error_message") else None,
            node_history=list(values.get("node_history", [])),
        )

    def _sync_projection(
        self,
        *,
        run: WorkflowRunModel,
        checkpoint_snapshot: Any,
    ) -> WorkflowExecutionSnapshot:
        status, current_node, _interrupted, _payload = _derive_status_from_snapshot(
            checkpoint_snapshot
        )
        run.status = status
        run.current_node = current_node
        self._db.flush()
        return self._build_snapshot_response(
            run=run,
            checkpoint_snapshot=checkpoint_snapshot,
            reused_existing=False,
        )

    def start_workflow(
        self,
        *,
        repository: RepositoryModel,
        base_branch: str,
        base_commit_sha: str,
        workspace_dir: Path,
        title: str,
        request_text: str,
        idempotency_key: str,
        cost_limit_usd: Decimal,
        actor: str,
    ) -> WorkflowExecutionSnapshot:
        """Start a version-pinned vertical-slice workflow idempotently until pause or end."""
        if cost_limit_usd <= Decimal("0"):
            raise ValidationFailedError(
                "A positive cost_limit_usd is mandatory to start any workflow.",
                details={"cost_limit_usd": str(cost_limit_usd)},
            )

        existing = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.idempotency_key == idempotency_key)
        )
        if existing is not None:
            config = {"configurable": {"thread_id": existing.thread_id}}
            existing_snapshot = self._graph.get_state(config)
            return self._build_snapshot_response(
                run=existing,
                checkpoint_snapshot=existing_snapshot,
                reused_existing=True,
            )

        thread_id = f"wf-{uuid.uuid4().hex}"
        run = WorkflowRunModel(
            repository_id=repository.id,
            base_branch=base_branch,
            base_commit_sha=base_commit_sha,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            title=title,
            request_text=request_text,
            status="running",
            current_node="requirement_analysis",
            cost_limit_usd=cost_limit_usd,
            created_by=actor,
        )
        self._db.add(run)
        self._db.flush()

        initial_state: WorkflowState = {
            "state_schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
            "workflow_id": run.id,
            "thread_id": thread_id,
            "repository_id": repository.id,
            "repository_full_name": repository.full_name,
            "is_private_repo": repository.is_private,
            "llm_data_sharing": repository.llm_data_sharing,
            "base_branch": base_branch,
            "base_commit_sha": base_commit_sha,
            "workspace_dir": str(workspace_dir.resolve()),
            "title": title,
            "request_text": request_text,
            "cost_limit_usd": str(cost_limit_usd),
            "status": "running",
            "current_node": "requirement_analysis",
            "started_at_iso": datetime.now(UTC).isoformat(),
            "llm_calls_count": 0,
            "total_tokens": 0,
            "estimated_cost_usd": "0.000000",
            "cost_status": "estimated",
            "clarification_attempts": 0,
            "clarification_answers": None,
            "plan_approved": False,
            "approved_protected_paths": [],
            "usage_records": [],
            "node_history": [],
        }

        config = {"configurable": {"thread_id": thread_id}}
        self._graph.invoke(initial_state, config=config)
        post_snapshot = self._graph.get_state(config)
        response = self._sync_projection(run=run, checkpoint_snapshot=post_snapshot)

        self._audit.record(
            actor=actor,
            action="workflow.start",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "repository_id": repository.id,
                "base_commit_sha": base_commit_sha,
                "status": response.status,
                "cost_limit_usd": str(cost_limit_usd),
            },
        )
        return response

    def resume_workflow(
        self,
        *,
        workflow_id: str,
        decision: HumanDecision,
    ) -> WorkflowExecutionSnapshot:
        """Resume a paused workflow from its authoritative LangGraph checkpoint."""
        run = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        )
        if run is None:
            raise ResourceNotFoundError(
                "Workflow run not found.",
                details={"workflow_id": workflow_id},
            )

        config = {"configurable": {"thread_id": run.thread_id}}
        current_snapshot = self._graph.get_state(config)
        values: dict[str, Any] = dict(getattr(current_snapshot, "values", {}) or {})
        if not values:
            raise ConflictError(
                "No LangGraph checkpoint found for workflow.",
                details={"workflow_id": workflow_id},
            )

        # Safety invariant: never resume an incompatible checkpoint state schema
        validate_state_schema_version(values)

        pending_next = tuple(getattr(current_snapshot, "next", ()) or ())
        if not pending_next:
            raise ConflictError(
                "Workflow is not currently paused at a human interrupt.",
                details={"workflow_id": workflow_id, "status": run.status},
            )

        self._graph.invoke(
            Command(resume=decision.model_dump(mode="json")),
            config=config,
        )
        post_snapshot = self._graph.get_state(config)
        response = self._sync_projection(run=run, checkpoint_snapshot=post_snapshot)

        self._audit.record(
            actor=decision.actor,
            action=f"workflow.resume.{decision.action}",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "base_commit_sha": run.base_commit_sha,
                "approved_plan_hash": decision.approved_plan_hash,
                "resulting_status": response.status,
            },
        )
        return response

    def reconcile_projection(self, workflow_id: str) -> WorkflowExecutionSnapshot:
        """Repair any projection drift in WorkflowRunModel from the LangGraph checkpoint."""
        run = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        )
        if run is None:
            raise ResourceNotFoundError(
                "Workflow run not found.",
                details={"workflow_id": workflow_id},
            )

        config = {"configurable": {"thread_id": run.thread_id}}
        snapshot = self._graph.get_state(config)
        values: dict[str, Any] = dict(getattr(snapshot, "values", {}) or {})
        if not values:
            raise ConflictError(
                "Cannot reconcile projection without an existing LangGraph checkpoint.",
                details={"workflow_id": workflow_id},
            )

        return self._sync_projection(run=run, checkpoint_snapshot=snapshot)