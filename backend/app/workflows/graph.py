"""Deterministic LangGraph StateGraph orchestrating the full seven-agent SDLC workflow.

Architecture invariants:
- LangGraph checkpoints are the single source of truth for execution state.
- WorkflowRunModel, governance tables, and workflow_events are projections synced idempotently.
- Routing is strictly deterministic Python (no LLM-based Supervisor).
- Human approvals bind explicitly to (plan_hash, base_commit_sha) and (patch_hash, base_commit_sha).
- Bounded correction loops guarantee infinite retries are impossible.
"""

import contextlib
import difflib
import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from git import InvalidGitRepositoryError, Repo
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.code_review_agent import CodeReviewAgent
from app.agents.coding_agent import CodingAgent
from app.agents.planning_agent import PlanningAgent
from app.agents.release_agent import ReleaseAgent
from app.agents.repository_analysis_agent import RepositoryAnalysisAgent
from app.agents.requirement_agent import RequirementAnalysisAgent
from app.agents.schemas import (
    CodeReviewReport,
    ImplementationPlan,
    RepositoryAnalysisReport,
    RequirementSpecification,
    TechnicalDesign,
)
from app.agents.testing_agent import TestingAgent
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from app.execution.patch_service import PatchService, classify_protected_path
from app.execution.sandbox_client import SandboxClient, SandboxRunReport
from app.execution.schemas import (
    PatchOperationType,
    PatchValidationResult,
    ProtectedPathFlag,
    TestIntegrityFlag,
)
from app.execution.static_analysis import StaticAnalysisReport, StaticAnalysisService
from app.execution.test_integrity import inspect_test_integrity
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.intelligence.retrieval import HybridRetrievalService
from app.models.repository import RepositoryModel
from app.models.workflow import WorkflowRunModel
from app.observability.tracing import build_langgraph_run_config
from app.services.audit_service import AuditService
from app.services.delivery_service import ReleaseDeliveryService
from app.services.event_service import WorkflowEventService
from app.workflows.governance import WorkflowGovernanceRecorder
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
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "rejected", "cancelled"}
)


def _append_history(state: WorkflowState, node_name: str) -> list[str]:
    return [*state.get("node_history", []), node_name]


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
        with contextlib.suppress(ValueError):
            started_dt = datetime.fromisoformat(started_iso)
            elapsed = (datetime.now(UTC) - started_dt).total_seconds()
            if elapsed > settings.WORKFLOW_TIMEOUT_SECONDS:
                return (
                    "WORKFLOW_TIMEOUT",
                    f"Workflow exceeded wall-clock limit of {settings.WORKFLOW_TIMEOUT_SECONDS}s.",
                )

    return None


def _compute_cumulative_validation(
    *,
    workspace_dir: Path,
    base_commit_sha: str,
    touched_files: list[str],
    expected_files: list[str],
    declared_protected_paths: list[str],
    approved_protected_paths: list[str],
    plan_declares_dependency_change: bool,
    settings: Settings,
) -> PatchValidationResult:
    """Compute net cumulative diff, flags, and patch_hash against immutable base_commit_sha."""
    root = workspace_dir.resolve()
    try:
        repo = Repo(str(root))
    except InvalidGitRepositoryError as exc:
        raise ValidationFailedError(
            "Workspace is not a valid Git repository.",
            details={"workspace_dir": str(root)},
        ) from exc

    try:
        head_sha = str(repo.head.commit.hexsha)
        if head_sha != base_commit_sha:
            raise ConflictError(
                "Workspace HEAD drifted from workflow base_commit_sha.",
                details={"expected_base_sha": base_commit_sha, "actual_base_sha": head_sha},
            )

        expected_set = set(expected_files)
        declared_protected_set = set(declared_protected_paths)
        approved_protected_set = set(approved_protected_paths)

        diff_chunks: list[str] = []
        changed_files: list[str] = []
        scope_drift_files: list[str] = []
        protected_flags: list[ProtectedPathFlag] = []
        test_integrity_flags: list[TestIntegrityFlag] = []
        lines_added = 0
        lines_removed = 0

        for rel_path in sorted(set(touched_files)):
            old_text: str | None = None
            try:
                blob = repo.head.commit.tree / rel_path
                raw_old = bytes(blob.data_stream.read())
                old_text = _normalize_newlines(raw_old.decode("utf-8"))
            except KeyError:
                old_text = None

            disk_path = (root / rel_path).resolve()
            new_text: str | None = None
            if disk_path.is_relative_to(root) and disk_path.is_file():
                new_text = _normalize_newlines(disk_path.read_text(encoding="utf-8"))

            if old_text == new_text:
                continue

            changed_files.append(rel_path)
            if rel_path not in expected_set:
                scope_drift_files.append(rel_path)

            operation: PatchOperationType
            if old_text is None:
                operation = "create"
            elif new_text is None:
                operation = "delete"
            else:
                operation = "modify"

            protected_match = classify_protected_path(rel_path)
            if protected_match is not None:
                category, reason = protected_match
                is_declared = rel_path in declared_protected_set or (
                    category == "dependency_manifest" and plan_declares_dependency_change
                )
                protected_flags.append(
                    ProtectedPathFlag(
                        file_path=rel_path,
                        category=category,
                        reason=reason,
                        declared_in_plan=is_declared,
                        approved=rel_path in approved_protected_set,
                    )
                )

            test_integrity_flags.extend(
                inspect_test_integrity(
                    file_path=rel_path,
                    operation=operation,
                    old_content=old_text,
                    new_content=new_text,
                )
            )

            old_lines = old_text.splitlines(keepends=True) if old_text is not None else []
            new_lines = new_text.splitlines(keepends=True) if new_text is not None else []
            fromfile = "/dev/null" if operation == "create" else f"a/{rel_path}"
            tofile = "/dev/null" if operation == "delete" else f"b/{rel_path}"
            file_diff = list(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=fromfile,
                    tofile=tofile,
                    lineterm="\n",
                )
            )
            for line in file_diff:
                if line.startswith("+") and not line.startswith("+++"):
                    lines_added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    lines_removed += 1
            if file_diff:
                diff_chunks.append(
                    "".join(line if line.endswith("\n") else f"{line}\n" for line in file_diff)
                )
    finally:
        repo.close()

    unified_diff = "".join(diff_chunks)
    total_changed_lines = lines_added + lines_removed
    errors: list[str] = []

    if len(changed_files) > settings.MAX_FILES_PER_PATCH:
        errors.append(
            f"Cumulative changes touch {len(changed_files)} files, exceeding limit "
            f"{settings.MAX_FILES_PER_PATCH}."
        )
    if total_changed_lines > settings.MAX_LINES_PER_PATCH:
        errors.append(
            f"Cumulative changes span {total_changed_lines} lines, exceeding limit "
            f"{settings.MAX_LINES_PER_PATCH}."
        )

    unapproved_protected = [
        f for f in protected_flags if not (f.declared_in_plan and f.approved)
    ]
    if unapproved_protected:
        errors.append(
            "Cumulative patch touches protected path(s) without plan declaration and approval."
        )
    if test_integrity_flags:
        errors.append("Cumulative patch triggers test-integrity warning(s).")

    patch_hash = hashlib.sha256(
        f"{base_commit_sha}\n{unified_diff}".encode()
    ).hexdigest()
    return PatchValidationResult(
        valid=len(errors) == 0,
        base_commit_sha=base_commit_sha,
        patch_hash=patch_hash,
        unified_diff=unified_diff,
        files_changed=changed_files,
        lines_added=lines_added,
        lines_removed=lines_removed,
        total_changed_lines=total_changed_lines,
        protected_path_flags=protected_flags,
        scope_drift_files=scope_drift_files,
        test_integrity_flags=test_integrity_flags,
        requires_human_approval=bool(unapproved_protected or test_integrity_flags),
        errors=errors,
    )


def _format_review_correction_feedback(
    report: CodeReviewReport,
    static_report: StaticAnalysisReport | None = None,
) -> str:
    lines: list[str] = [
        "Code Review failed. Address ALL blocking defects and re-verify:",
        f"Review Summary: {report.summary}",
    ]
    if report.blocking_reasons:
        lines.append("Blocking Reasons:")
        lines.extend(f"- {reason}" for reason in report.blocking_reasons)
    for finding in report.findings:
        if finding.severity in {"Critical", "High", "Medium"}:
            lines.append(
                f"- [{finding.severity}/{finding.classification}] {finding.file_path}: "
                f"{finding.title} -> {finding.recommendation}"
            )
    if static_report is not None and static_report.findings:
        blocking_static = [
            f
            for f in static_report.findings
            if f.severity in {"Critical", "High"}
            or (f.tool == "mypy" and f.severity == "Medium")
        ]
        if blocking_static:
            lines.append("Blocking Deterministic Static Analysis Findings:")
            for sf in blocking_static[:20]:
                loc = f":{sf.start_line}" if sf.start_line is not None else ""
                lines.append(
                    f"- [{sf.tool}][{sf.severity}][{sf.rule_id}] "
                    f"{sf.file_path}{loc}: {sf.message}"
                )
    return "\n".join(lines)


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
    current_node_val = values.get("current_node")
    current_node = str(current_node_val) if current_node_val is not None else None

    if values.get("status") == "cancelled":
        return "cancelled", current_node, False, None

    pending_next = tuple(getattr(snapshot, "next", ()) or ())
    interrupt_payload = _extract_interrupt_payload(snapshot)
    is_interrupted = bool(pending_next)

    if is_interrupted:
        intr_type = interrupt_payload.get("type") if interrupt_payload else None
        if intr_type == "clarification_required":
            return "waiting_clarification", "human_clarification", True, interrupt_payload
        if intr_type == "plan_approval_required":
            return "waiting_plan_approval", "human_plan_approval", True, interrupt_payload
        if intr_type == "commit_approval_required":
            return "waiting_commit_approval", "human_commit_approval", True, interrupt_payload
        next_node = str(pending_next[0]) if pending_next else current_node
        return "running", next_node, True, interrupt_payload

    status = str(values.get("status", "failed"))
    return status, current_node, False, None


def build_sdlc_workflow_graph(
    *,
    db: Session,
    llm_client: LLMClient,
    retrieval_service: HybridRetrievalService,
    patch_service: PatchService,
    sandbox_client: SandboxClient,
    checkpointer: Any,
    static_analysis_service: StaticAnalysisService | None = None,
    delivery_service: ReleaseDeliveryService | None = None,
    settings: Settings | None = None,
) -> Any:
    """Compile the deterministic seven-agent SDLC LangGraph StateGraph."""
    resolved_settings = settings or get_settings()
    resolved_static = static_analysis_service or StaticAnalysisService(
        sandbox_client=sandbox_client
    )
    req_agent = RequirementAnalysisAgent(llm_client)
    repo_agent = RepositoryAnalysisAgent(llm_client, retrieval_service)
    plan_agent = PlanningAgent(llm_client)
    coding_agent = CodingAgent(llm_client, patch_service)
    testing_agent = TestingAgent(llm_client)
    review_agent = CodeReviewAgent(llm_client)
    release_agent = ReleaseAgent(llm_client)

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
        if state.get("status") in _TERMINAL_STATUSES:
            return "end"
        raw_spec = state.get("requirement_spec")
        if not raw_spec:
            return "end"
        if bool(raw_spec.get("needs_clarification")):
            if int(state.get("clarification_attempts", 0)) >= MAX_CLARIFICATION_ATTEMPTS:
                return "fail_clarification_limit"
            return "human_clarification"
        return "repository_analysis"

    def fail_clarification_limit_node(state: WorkflowState) -> dict[str, Any]:
        return {
            "status": "failed",
            "current_node": "fail_clarification_limit",
            "error_code": "CLARIFICATION_LIMIT_EXCEEDED",
            "error_message": (
                f"Requirement remained ambiguous after {MAX_CLARIFICATION_ATTEMPTS} "
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
        if state.get("status") in _TERMINAL_STATUSES:
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
            if state.get("status") in _TERMINAL_STATUSES:
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
            feedback = state.get("pending_correction_feedback")

            coding_res = coding_agent.run(
                workspace_dir=workspace_path,
                base_commit_sha=state["base_commit_sha"],
                design=design,
                plan=plan,
                is_private_repo=state["is_private_repo"],
                llm_data_sharing=state["llm_data_sharing"],
                feedback=feedback,
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
            merged_touched = sorted(
                set(state.get("touched_files", [])).union(applied.files_changed)
            )
            cumulative_validation = _compute_cumulative_validation(
                workspace_dir=workspace_path,
                base_commit_sha=state["base_commit_sha"],
                touched_files=merged_touched,
                expected_files=plan.expected_files,
                declared_protected_paths=plan.declared_protected_paths,
                approved_protected_paths=approved_protected,
                plan_declares_dependency_change=plan.declares_dependency_change,
                settings=resolved_settings,
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

        cumulative_dump = cumulative_validation.model_dump(mode="json")
        change_history = [*state.get("code_change_history", []), cumulative_dump]

        if not cumulative_validation.valid:
            return {
                **usage_update,
                "status": "failed",
                "current_node": "coding",
                "touched_files": merged_touched,
                "coding_output": coding_res.output.model_dump(mode="json"),
                "patch_validation": cumulative_dump,
                "patch_apply_result": applied.model_dump(mode="json"),
                "patch_hash": cumulative_validation.patch_hash,
                "code_change_history": change_history,
                "error_code": "PATCH_VALIDATION_FAILED",
                "error_message": "; ".join(cumulative_validation.errors),
                "node_history": _append_history(state, "coding"),
            }

        return {
            **usage_update,
            "status": "running",
            "current_node": "coding",
            "pending_correction_feedback": None,
            "commit_approved": False,
            "touched_files": merged_touched,
            "coding_output": coding_res.output.model_dump(mode="json"),
            "patch_validation": cumulative_dump,
            "patch_apply_result": applied.model_dump(mode="json"),
            "patch_hash": cumulative_validation.patch_hash,
            "code_change_history": change_history,
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

        report_dump = report.model_dump(mode="json")
        return {
            "status": "running",
            "current_node": "sandbox_test",
            "sandbox_report": report_dump,
            "test_execution_history": [*state.get("test_execution_history", []), report_dump],
            "node_history": _append_history(state, "sandbox_test"),
        }

    def route_after_sandbox_test(state: WorkflowState) -> str:
        if state.get("status") in _TERMINAL_STATUSES:
            return "end"
        sandbox_raw = state.get("sandbox_report")
        if isinstance(sandbox_raw, dict) and bool(sandbox_raw.get("passed")):
            return "static_analysis"
        return "test_failure_analysis"

    def static_analysis_node(state: WorkflowState) -> dict[str, Any]:
        try:
            report = resolved_static.run(
                workflow_id=state["workflow_id"],
                repository_id=state["repository_id"],
                base_commit_sha=state["base_commit_sha"],
                workspace_dir=Path(state["workspace_dir"]),
            )
        except AppError as exc:
            return {
                "status": "failed",
                "current_node": "static_analysis",
                "error_code": exc.code,
                "error_message": exc.message,
                "node_history": _append_history(state, "static_analysis"),
            }

        report_dump = report.model_dump(mode="json")
        return {
            "status": "running",
            "current_node": "static_analysis",
            "static_analysis_report": report_dump,
            "static_analysis_summary": report.summary_text,
            "static_analysis_history": [*state.get("static_analysis_history", []), report_dump],
            "node_history": _append_history(state, "static_analysis"),
        }

    def test_failure_analysis_node(state: WorkflowState) -> dict[str, Any]:
        test_loops = int(state.get("test_correction_loops", 0))
        total_loops = int(state.get("total_correction_loops", 0))
        if (
            test_loops >= resolved_settings.MAX_CORRECTION_LOOPS_PER_FAILURE
            or total_loops >= resolved_settings.MAX_CORRECTION_LOOPS_PER_WORKFLOW
        ):
            return {
                "status": "failed",
                "current_node": "test_failure_analysis",
                "error_code": "CORRECTION_LIMIT_EXCEEDED",
                "error_message": (
                    "Test failure correction loop limit reached "
                    f"(test_loops={test_loops}, total_loops={total_loops})."
                ),
                "node_history": _append_history(state, "test_failure_analysis"),
            }

        budget_violation = _check_budget_limits(state, resolved_settings)
        if budget_violation is not None:
            return {
                "status": "failed",
                "current_node": "test_failure_analysis",
                "error_code": budget_violation[0],
                "error_message": budget_violation[1],
                "node_history": _append_history(state, "test_failure_analysis"),
            }

        try:
            requirement = RequirementSpecification.model_validate(state["requirement_spec"])
            plan = ImplementationPlan.model_validate(state["implementation_plan"])
            patch_val = PatchValidationResult.model_validate(state["patch_validation"])
            sandbox_rep = SandboxRunReport.model_validate(state["sandbox_report"])
            result = testing_agent.analyze_failure(
                requirement=requirement,
                plan=plan,
                raw_diff=patch_val.unified_diff,
                sandbox_report=sandbox_rep,
                is_private_repo=state["is_private_repo"],
                llm_data_sharing=state["llm_data_sharing"],
                attempt_number=test_loops + 1,
            )
        except (AppError, ValidationError) as exc:
            err_code = exc.code if isinstance(exc, AppError) else "STATE_VALIDATION_ERROR"
            err_msg = exc.message if isinstance(exc, AppError) else str(exc)
            return {
                "status": "failed",
                "current_node": "test_failure_analysis",
                "error_code": err_code,
                "error_message": err_msg,
                "node_history": _append_history(state, "test_failure_analysis"),
            }

        usage_update = _accumulate_usage(state, result.usage)
        if not result.diagnosis.is_fixable_by_coding_agent:
            return {
                **usage_update,
                "status": "failed",
                "current_node": "test_failure_analysis",
                "last_test_diagnosis": result.diagnosis.model_dump(mode="json"),
                "error_code": "UNFIXABLE_TEST_FAILURE",
                "error_message": result.diagnosis.summary,
                "node_history": _append_history(state, "test_failure_analysis"),
            }

        return {
            **usage_update,
            "status": "running",
            "current_node": "test_failure_analysis",
            "last_test_diagnosis": result.diagnosis.model_dump(mode="json"),
            "test_correction_loops": test_loops + 1,
            "total_correction_loops": total_loops + 1,
            "pending_correction_feedback": (
                f"Test failure diagnosis ({result.diagnosis.failure_category}): "
                f"{result.diagnosis.root_cause}\n"
                f"Correction instructions: {result.diagnosis.correction_instructions}"
            ),
            "node_history": _append_history(state, "test_failure_analysis"),
        }

    def code_review_node(state: WorkflowState) -> dict[str, Any]:
        budget_violation = _check_budget_limits(state, resolved_settings)
        if budget_violation is not None:
            return {
                "status": "failed",
                "current_node": "code_review",
                "error_code": budget_violation[0],
                "error_message": budget_violation[1],
                "node_history": _append_history(state, "code_review"),
            }

        static_rep: StaticAnalysisReport | None = None
        try:
            requirement = RequirementSpecification.model_validate(state["requirement_spec"])
            plan = ImplementationPlan.model_validate(state["implementation_plan"])
            patch_val = PatchValidationResult.model_validate(state["patch_validation"])
            sandbox_rep = SandboxRunReport.model_validate(state["sandbox_report"])
            raw_static = state.get("static_analysis_report")
            if isinstance(raw_static, dict):
                static_rep = StaticAnalysisReport.model_validate(raw_static)

            result = review_agent.run(
                requirement=requirement,
                plan=plan,
                patch_validation=patch_val,
                sandbox_report=sandbox_rep,
                static_analysis_report=static_rep,
                static_analysis_summary=state.get("static_analysis_summary"),
                is_private_repo=state["is_private_repo"],
                llm_data_sharing=state["llm_data_sharing"],
            )
        except (AppError, ValidationError) as exc:
            err_code = exc.code if isinstance(exc, AppError) else "STATE_VALIDATION_ERROR"
            err_msg = exc.message if isinstance(exc, AppError) else str(exc)
            return {
                "status": "failed",
                "current_node": "code_review",
                "error_code": err_code,
                "error_message": err_msg,
                "node_history": _append_history(state, "code_review"),
            }

        usage_update = _accumulate_usage(state, result.usage)
        review_dump = result.report.model_dump(mode="json")
        review_record = {
            **review_dump,
            "base_commit_sha": state["base_commit_sha"],
            "patch_hash": state.get("patch_hash"),
        }
        review_history = [*state.get("code_review_history", []), review_record]

        if result.report.passed:
            return {
                **usage_update,
                "status": "running",
                "current_node": "code_review",
                "code_review_report": review_dump,
                "code_review_history": review_history,
                "pending_correction_feedback": None,
                "node_history": _append_history(state, "code_review"),
            }

        review_loops = int(state.get("review_correction_loops", 0))
        total_loops = int(state.get("total_correction_loops", 0))
        if (
            review_loops >= resolved_settings.MAX_CORRECTION_LOOPS_PER_FAILURE
            or total_loops >= resolved_settings.MAX_CORRECTION_LOOPS_PER_WORKFLOW
        ):
            return {
                **usage_update,
                "status": "failed",
                "current_node": "code_review",
                "code_review_report": review_dump,
                "code_review_history": review_history,
                "error_code": "CORRECTION_LIMIT_EXCEEDED",
                "error_message": (
                    "Code review correction loop limit reached "
                    f"(review_loops={review_loops}, total_loops={total_loops})."
                ),
                "node_history": _append_history(state, "code_review"),
            }

        return {
            **usage_update,
            "status": "running",
            "current_node": "code_review",
            "code_review_report": review_dump,
            "code_review_history": review_history,
            "review_correction_loops": review_loops + 1,
            "total_correction_loops": total_loops + 1,
            "pending_correction_feedback": _format_review_correction_feedback(
                result.report,
                static_rep,
            ),
            "node_history": _append_history(state, "code_review"),
        }

    def route_after_code_review(state: WorkflowState) -> str:
        if state.get("status") in _TERMINAL_STATUSES:
            return "end"
        review_raw = state.get("code_review_report")
        if isinstance(review_raw, dict) and bool(review_raw.get("passed")):
            return "human_commit_approval"
        return "coding"

    def human_commit_approval_node(state: WorkflowState) -> dict[str, Any]:
        patch_val = state.get("patch_validation") or {}
        decision_raw = interrupt(
            {
                "type": "commit_approval_required",
                "workflow_id": state["workflow_id"],
                "base_commit_sha": state["base_commit_sha"],
                "patch_hash": state.get("patch_hash"),
                "raw_diff": patch_val.get("unified_diff", ""),
                "files_changed": list(patch_val.get("files_changed", [])),
                "protected_path_flags": list(patch_val.get("protected_path_flags", [])),
                "test_integrity_flags": list(patch_val.get("test_integrity_flags", [])),
                "scope_drift_files": list(patch_val.get("scope_drift_files", [])),
                "sandbox_report": state.get("sandbox_report"),
                "static_analysis_report": state.get("static_analysis_report"),
                "code_review_report": state.get("code_review_report"),
                "estimated_cost_usd": state.get("estimated_cost_usd", "0.000000"),
                "cost_status": state.get("cost_status", "unavailable"),
            }
        )

        try:
            decision = HumanDecision.model_validate(decision_raw)
        except ValidationError as exc:
            return {
                "status": "failed",
                "current_node": "human_commit_approval",
                "error_code": "INVALID_HUMAN_DECISION",
                "error_message": f"Invalid commit approval payload: {exc.error_count()} error(s).",
                "node_history": _append_history(state, "human_commit_approval"),
            }

        if decision.action == "reject":
            return {
                "status": "rejected",
                "current_node": "human_commit_approval",
                "commit_approved": False,
                "commit_approval_record": {
                    "action": "reject",
                    "actor": decision.actor,
                    "reason": decision.reason,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "patch_hash": state.get("patch_hash"),
                    "base_commit_sha": state["base_commit_sha"],
                },
                "node_history": _append_history(state, "human_commit_approval"),
            }

        if decision.action != "approve":
            return {
                "status": "failed",
                "current_node": "human_commit_approval",
                "error_code": "UNEXPECTED_HUMAN_ACTION",
                "error_message": f"Expected 'approve' or 'reject', got '{decision.action}'.",
                "node_history": _append_history(state, "human_commit_approval"),
            }

        if (
            decision.approved_patch_hash != state.get("patch_hash")
            or decision.approved_base_commit_sha != state["base_commit_sha"]
        ):
            return {
                "status": "failed",
                "current_node": "human_commit_approval",
                "commit_approved": False,
                "error_code": "APPROVAL_BINDING_MISMATCH",
                "error_message": (
                    "Commit/PR approval patch_hash or base_commit_sha does not match current state."
                ),
                "node_history": _append_history(state, "human_commit_approval"),
            }

        return {
            "status": "running",
            "current_node": "human_commit_approval",
            "commit_approved": True,
            "commit_approval_record": {
                "action": "approve",
                "actor": decision.actor,
                "recorded_at": datetime.now(UTC).isoformat(),
                "patch_hash": state.get("patch_hash"),
                "base_commit_sha": state["base_commit_sha"],
            },
            "node_history": _append_history(state, "human_commit_approval"),
        }

    def route_after_commit_approval(state: WorkflowState) -> str:
        if state.get("commit_approved") is True and state.get("status") == "running":
            return "release_preparation"
        return "end"

    def release_preparation_node(state: WorkflowState) -> dict[str, Any]:
        if state.get("commit_approved") is not True:
            return {
                "status": "failed",
                "current_node": "release_preparation",
                "error_code": "COMMIT_APPROVAL_REQUIRED",
                "error_message": "Release preparation requires recorded human Commit/PR approval.",
                "node_history": _append_history(state, "release_preparation"),
            }

        budget_violation = _check_budget_limits(state, resolved_settings)
        if budget_violation is not None:
            return {
                "status": "failed",
                "current_node": "release_preparation",
                "error_code": budget_violation[0],
                "error_message": budget_violation[1],
                "node_history": _append_history(state, "release_preparation"),
            }

        try:
            requirement = RequirementSpecification.model_validate(state["requirement_spec"])
            plan = ImplementationPlan.model_validate(state["implementation_plan"])
            patch_val = PatchValidationResult.model_validate(state["patch_validation"])
            sandbox_rep = SandboxRunReport.model_validate(state["sandbox_report"])
            review_rep = CodeReviewReport.model_validate(state["code_review_report"])
            result = release_agent.run(
                repository_full_name=state["repository_full_name"],
                base_branch=state["base_branch"],
                requirement=requirement,
                plan=plan,
                patch_validation=patch_val,
                sandbox_report=sandbox_rep,
                review_report=review_rep,
                static_analysis_summary=state.get("static_analysis_summary"),
                is_private_repo=state["is_private_repo"],
                llm_data_sharing=state["llm_data_sharing"],
            )
        except (AppError, ValidationError) as exc:
            err_code = exc.code if isinstance(exc, AppError) else "STATE_VALIDATION_ERROR"
            err_msg = exc.message if isinstance(exc, AppError) else str(exc)
            return {
                "status": "failed",
                "current_node": "release_preparation",
                "error_code": err_code,
                "error_message": err_msg,
                "node_history": _append_history(state, "release_preparation"),
            }

        usage_update = _accumulate_usage(state, result.usage)
        release_dump = result.output.model_dump(mode="json")
        artifact_ids: list[str] = list(state.get("artifact_ids", []))
        pr_delivery_dump: dict[str, Any] | None = None

        if delivery_service is not None:
            created_by = str(state.get("created_by") or "operator")
            approval_rec = state.get("commit_approval_record")
            approver = (
                str(approval_rec["actor"])
                if isinstance(approval_rec, dict) and approval_rec.get("actor")
                else created_by
            )
            workspace_id = str(state.get("workspace_id") or Path(state["workspace_dir"]).name)
            patch_hash = str(state.get("patch_hash") or patch_val.patch_hash)

            try:
                artifact_ids = delivery_service.persist_workflow_artifacts(
                    workflow_id=state["workflow_id"],
                    created_by=created_by,
                    unified_diff=patch_val.unified_diff,
                    sandbox_report=state.get("sandbox_report"),
                    static_analysis_report=state.get("static_analysis_report"),
                    code_review_report=state.get("code_review_report"),
                    release_output=release_dump,
                )
            except AppError as exc:
                return {
                    **usage_update,
                    "status": "failed",
                    "current_node": "release_preparation",
                    "release_output": release_dump,
                    "error_code": exc.code,
                    "error_message": exc.message,
                    "node_history": _append_history(state, "release_preparation"),
                }

            try:
                pr_delivery = delivery_service.deliver_pull_request(
                    workflow_id=state["workflow_id"],
                    workspace_id=workspace_id,
                    base_commit_sha=state["base_commit_sha"],
                    patch_hash=patch_hash,
                    commit_approved=True,
                    release_output=result.output,
                    actor=approver,
                )
                pr_delivery_dump = pr_delivery.model_dump(mode="json")
            except AppError as exc:
                return {
                    **usage_update,
                    "status": "failed",
                    "current_node": "release_preparation",
                    "release_output": release_dump,
                    "artifact_ids": artifact_ids,
                    "error_code": exc.code,
                    "error_message": exc.message,
                    "node_history": _append_history(state, "release_preparation"),
                }

        return {
            **usage_update,
            "status": "completed",
            "current_node": "release_preparation",
            "release_output": release_dump,
            "pull_request_delivery": pr_delivery_dump,
            "artifact_ids": artifact_ids,
            "node_history": _append_history(state, "release_preparation"),
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
    graph_builder.add_node("static_analysis", static_analysis_node)
    graph_builder.add_node("test_failure_analysis", test_failure_analysis_node)
    graph_builder.add_node("code_review", code_review_node)
    graph_builder.add_node("human_commit_approval", human_commit_approval_node)
    graph_builder.add_node("release_preparation", release_preparation_node)

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
        {"requirement_analysis": "requirement_analysis", "end": END},
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
    graph_builder.add_conditional_edges(
        "sandbox_test",
        route_after_sandbox_test,
        {
            "static_analysis": "static_analysis",
            "test_failure_analysis": "test_failure_analysis",
            "end": END,
        },
    )
    graph_builder.add_conditional_edges(
        "static_analysis",
        route_if_running("code_review"),
        {"code_review": "code_review", "end": END},
    )
    graph_builder.add_conditional_edges(
        "test_failure_analysis",
        route_if_running("coding"),
        {"coding": "coding", "end": END},
    )
    graph_builder.add_conditional_edges(
        "code_review",
        route_after_code_review,
        {
            "human_commit_approval": "human_commit_approval",
            "coding": "coding",
            "end": END,
        },
    )
    graph_builder.add_conditional_edges(
        "human_commit_approval",
        route_after_commit_approval,
        {"release_preparation": "release_preparation", "end": END},
    )
    graph_builder.add_edge("release_preparation", END)

    return graph_builder.compile(checkpointer=checkpointer)


class WorkflowOrchestrator:
    """Runs, resumes, cancels, and reconciles workflows while streaming projections/events."""

    def __init__(
        self,
        db: Session,
        *,
        llm_client: LLMClient,
        retrieval_service: HybridRetrievalService,
        patch_service: PatchService,
        sandbox_client: SandboxClient,
        checkpointer: Any,
        static_analysis_service: StaticAnalysisService | None = None,
        delivery_service: ReleaseDeliveryService | None = None,
        on_progress: Callable[[], None] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._settings = settings or get_settings()
        self._audit = AuditService(db)
        self._governance = WorkflowGovernanceRecorder(db)
        self._events = WorkflowEventService(db)
        self._on_progress = on_progress
        self._graph = build_sdlc_workflow_graph(
            db=db,
            llm_client=llm_client,
            retrieval_service=retrieval_service,
            patch_service=patch_service,
            sandbox_client=sandbox_client,
            checkpointer=checkpointer,
            static_analysis_service=static_analysis_service,
            delivery_service=delivery_service,
            settings=self._settings,
        )

    def _build_initial_state(
        self,
        *,
        run: WorkflowRunModel,
        repository: RepositoryModel,
        workspace_dir: Path,
        tracing_enabled: bool,
        workspace_id: str | None = None,
    ) -> WorkflowState:
        resolved_ws = workspace_dir.resolve()
        return {
            "state_schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
            "workflow_id": run.id,
            "thread_id": run.thread_id,
            "repository_id": repository.id,
            "repository_full_name": repository.full_name,
            "is_private_repo": repository.is_private,
            "llm_data_sharing": repository.llm_data_sharing,
            "tracing_enabled": tracing_enabled,
            "base_branch": run.base_branch,
            "base_commit_sha": run.base_commit_sha,
            "workspace_id": workspace_id or resolved_ws.name,
            "workspace_dir": str(resolved_ws),
            "created_by": run.created_by,
            "title": run.title,
            "request_text": run.request_text,
            "cost_limit_usd": str(run.cost_limit_usd),
            "status": "running",
            "current_node": "requirement_analysis",
            "started_at_iso": datetime.now(UTC).isoformat(),
            "llm_calls_count": 0,
            "total_tokens": 0,
            "estimated_cost_usd": "0.000000",
            "cost_status": "estimated",
            "clarification_attempts": 0,
            "clarification_answers": None,
            "test_correction_loops": 0,
            "review_correction_loops": 0,
            "total_correction_loops": 0,
            "pending_correction_feedback": None,
            "plan_approved": False,
            "approved_protected_paths": [],
            "touched_files": [],
            "static_analysis_report": None,
            "static_analysis_summary": None,
            "code_change_history": [],
            "test_execution_history": [],
            "static_analysis_history": [],
            "code_review_history": [],
            "commit_approved": False,
            "pull_request_delivery": None,
            "artifact_ids": [],
            "usage_records": [],
            "node_history": [],
        }

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
        static_raw = values.get("static_analysis_report")
        static_passed = (
            bool(static_raw.get("passed")) if isinstance(static_raw, dict) else None
        )
        static_blocking = (
            int(static_raw.get("blocking_findings_count", 0))
            if isinstance(static_raw, dict)
            else 0
        )
        review_raw = values.get("code_review_report")
        review_passed = (
            bool(review_raw.get("passed")) if isinstance(review_raw, dict) else None
        )
        release_raw = values.get("release_output")
        pr_title = (
            str(release_raw.get("pr_title"))
            if isinstance(release_raw, dict) and release_raw.get("pr_title")
            else None
        )
        pr_delivery_raw = values.get("pull_request_delivery")
        pr_number = (
            int(pr_delivery_raw["pr_number"])
            if isinstance(pr_delivery_raw, dict) and pr_delivery_raw.get("pr_number")
            else None
        )
        pr_url = (
            str(pr_delivery_raw["html_url"])
            if isinstance(pr_delivery_raw, dict) and pr_delivery_raw.get("html_url")
            else None
        )
        patch_hash_val = values.get("patch_hash")
        if not patch_hash_val:
            patch_apply_raw = values.get("patch_apply_result")
            if isinstance(patch_apply_raw, dict):
                patch_hash_val = patch_apply_raw.get("patch_hash")

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
            patch_hash=str(patch_hash_val) if patch_hash_val else None,
            interrupted=interrupted,
            interrupt_payload=interrupt_payload,
            reused_existing=reused_existing,
            tracing_enabled=bool(values.get("tracing_enabled", False)),
            llm_calls_count=int(values.get("llm_calls_count", 0)),
            total_tokens=int(values.get("total_tokens", 0)),
            estimated_cost_usd=str(values.get("estimated_cost_usd", "0.000000")),
            cost_status=str(values.get("cost_status", "unavailable")),
            test_correction_loops=int(values.get("test_correction_loops", 0)),
            review_correction_loops=int(values.get("review_correction_loops", 0)),
            total_correction_loops=int(values.get("total_correction_loops", 0)),
            sandbox_passed=sandbox_passed,
            static_analysis_passed=static_passed,
            static_analysis_blocking_count=static_blocking,
            review_passed=review_passed,
            commit_approved=bool(values.get("commit_approved", False)),
            pr_title=pr_title,
            pr_number=pr_number,
            pr_url=pr_url,
            artifact_ids=[str(aid) for aid in values.get("artifact_ids", [])],
            error_code=str(values["error_code"]) if values.get("error_code") else None,
            error_message=str(values["error_message"]) if values.get("error_message") else None,
            node_history=list(values.get("node_history", [])),
        )

    def _sync_projection(
        self,
        *,
        run: WorkflowRunModel,
        checkpoint_snapshot: Any,
        reused_existing: bool = False,
    ) -> WorkflowExecutionSnapshot:
        values: dict[str, Any] = dict(getattr(checkpoint_snapshot, "values", {}) or {})
        status, current_node, _interrupted, interrupt_payload = _derive_status_from_snapshot(
            checkpoint_snapshot
        )
        run.status = status
        run.current_node = current_node
        if values:
            self._governance.sync_from_state(workflow_id=run.id, state=values)
            self._events.sync_transition_events(
                workflow_id=run.id,
                status=status,
                current_node=current_node,
                state=values,
                interrupt_payload=interrupt_payload,
            )
        self._db.flush()
        return self._build_snapshot_response(
            run=run,
            checkpoint_snapshot=checkpoint_snapshot,
            reused_existing=reused_existing,
        )

    def _run_graph_with_event_sync(
        self,
        *,
        run: WorkflowRunModel,
        graph_input: Any,
        invoke_config: dict[str, Any],
        lookup_config: dict[str, Any],
    ) -> WorkflowExecutionSnapshot:
        """Stream LangGraph supersteps so projections and SSE events update per completed node."""
        for chunk in self._graph.stream(
            graph_input,
            config=invoke_config,
            stream_mode="updates",
        ):
            if isinstance(chunk, dict) and "__interrupt__" in chunk:
                continue
            step_snapshot = self._graph.get_state(lookup_config)
            self._sync_projection(run=run, checkpoint_snapshot=step_snapshot)
            if self._on_progress is not None:
                self._on_progress()

        post_snapshot = self._graph.get_state(lookup_config)
        response = self._sync_projection(run=run, checkpoint_snapshot=post_snapshot)
        if self._on_progress is not None:
            self._on_progress()
        return response

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
        workspace_id: str | None = None,
    ) -> WorkflowExecutionSnapshot:
        """Start a version-pinned SDLC workflow idempotently until pause or completion."""
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

        config, tracing_enabled = build_langgraph_run_config(
            thread_id=thread_id,
            workflow_id=run.id,
            repository_id=repository.id,
            base_commit_sha=base_commit_sha,
            settings=self._settings,
            is_private_repo=repository.is_private,
            llm_data_sharing=repository.llm_data_sharing,
        )

        initial_state = self._build_initial_state(
            run=run,
            repository=repository,
            workspace_dir=workspace_dir,
            tracing_enabled=tracing_enabled,
            workspace_id=workspace_id,
        )
        response = self._run_graph_with_event_sync(
            run=run,
            graph_input=initial_state,
            invoke_config=config,
            lookup_config=config,
        )

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
                "tracing_enabled": tracing_enabled,
            },
        )
        return response

    def execute_queued_workflow(
        self,
        *,
        workflow_id: str,
        workspace_dir: Path,
        workspace_id: str | None = None,
    ) -> WorkflowExecutionSnapshot:
        """Execute a pre-created queued workflow idempotently inside a background worker."""
        run = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        )
        if run is None:
            raise ResourceNotFoundError(
                "Workflow run not found.",
                details={"workflow_id": workflow_id},
            )

        if run.status == "cancelled":
            return WorkflowExecutionSnapshot(
                workflow_id=run.id,
                thread_id=run.thread_id,
                state_schema_version=WORKFLOW_STATE_SCHEMA_VERSION,
                status="cancelled",
                current_node=run.current_node,
                base_commit_sha=run.base_commit_sha,
                reused_existing=True,
            )

        lookup_config = {"configurable": {"thread_id": run.thread_id}}
        existing_snapshot = self._graph.get_state(lookup_config)
        existing_values: dict[str, Any] = dict(getattr(existing_snapshot, "values", {}) or {})
        if existing_values:
            return self._sync_projection(
                run=run,
                checkpoint_snapshot=existing_snapshot,
                reused_existing=True,
            )

        repository = self._db.scalar(
            select(RepositoryModel).where(RepositoryModel.id == run.repository_id)
        )
        if repository is None:
            raise ResourceNotFoundError(
                "Repository linked to workflow not found.",
                details={"repository_id": run.repository_id, "workflow_id": run.id},
            )

        invoke_config, tracing_enabled = build_langgraph_run_config(
            thread_id=run.thread_id,
            workflow_id=run.id,
            repository_id=repository.id,
            base_commit_sha=run.base_commit_sha,
            settings=self._settings,
            is_private_repo=repository.is_private,
            llm_data_sharing=repository.llm_data_sharing,
        )
        initial_state = self._build_initial_state(
            run=run,
            repository=repository,
            workspace_dir=workspace_dir,
            tracing_enabled=tracing_enabled,
            workspace_id=workspace_id,
        )
        response = self._run_graph_with_event_sync(
            run=run,
            graph_input=initial_state,
            invoke_config=invoke_config,
            lookup_config=lookup_config,
        )

        self._audit.record(
            actor=run.created_by,
            action="workflow.worker_start",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "repository_id": repository.id,
                "base_commit_sha": run.base_commit_sha,
                "status": response.status,
                "tracing_enabled": tracing_enabled,
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

        lookup_config = {"configurable": {"thread_id": run.thread_id}}
        current_snapshot = self._graph.get_state(lookup_config)
        values: dict[str, Any] = dict(getattr(current_snapshot, "values", {}) or {})
        if not values:
            raise ConflictError(
                "No LangGraph checkpoint found for workflow.",
                details={"workflow_id": workflow_id},
            )

        if values.get("status") == "cancelled":
            raise ConflictError(
                "Cancelled workflow cannot be resumed.",
                details={"workflow_id": workflow_id},
            )

        validate_state_schema_version(values)

        pending_next = tuple(getattr(current_snapshot, "next", ()) or ())
        if not pending_next:
            raise ConflictError(
                "Workflow is not currently paused at a human interrupt.",
                details={"workflow_id": workflow_id, "status": run.status},
            )

        invoke_config, _ = build_langgraph_run_config(
            thread_id=run.thread_id,
            workflow_id=run.id,
            repository_id=run.repository_id,
            base_commit_sha=run.base_commit_sha,
            settings=self._settings,
            is_private_repo=bool(values.get("is_private_repo", True)),
            llm_data_sharing=bool(values.get("llm_data_sharing", False)),
        )

        response = self._run_graph_with_event_sync(
            run=run,
            graph_input=Command(resume=decision.model_dump(mode="json")),
            invoke_config=invoke_config,
            lookup_config=lookup_config,
        )

        self._audit.record(
            actor=decision.actor,
            action=f"workflow.resume.{decision.action}",
            resource_type="workflow",
            resource_id=run.id,
            details={
                "base_commit_sha": run.base_commit_sha,
                "approved_plan_hash": decision.approved_plan_hash,
                "approved_patch_hash": decision.approved_patch_hash,
                "resulting_status": response.status,
            },
        )
        return response

    def cancel_workflow(
        self,
        *,
        workflow_id: str,
        actor: str,
        reason: str | None = None,
    ) -> WorkflowExecutionSnapshot:
        """Cancel an active or paused workflow cleanly in both checkpoint and projection."""
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
            if run.status in _TERMINAL_STATUSES:
                raise ConflictError(
                    f"Workflow in terminal status '{run.status}' cannot be cancelled.",
                    details={"workflow_id": workflow_id, "status": run.status},
                )
            cancel_msg = reason or "Cancelled before worker start."
            run.status = "cancelled"
            self._events.sync_transition_events(
                workflow_id=run.id,
                status="cancelled",
                current_node=run.current_node,
                state={
                    "base_commit_sha": run.base_commit_sha,
                    "error_code": "WORKFLOW_CANCELLED",
                    "error_message": cancel_msg,
                },
                interrupt_payload=None,
            )
            self._db.flush()
            self._audit.record(
                actor=actor,
                action="workflow.cancel",
                resource_type="workflow",
                resource_id=run.id,
                details={"reason": cancel_msg},
            )
            return WorkflowExecutionSnapshot(
                workflow_id=run.id,
                thread_id=run.thread_id,
                state_schema_version=WORKFLOW_STATE_SCHEMA_VERSION,
                status="cancelled",
                current_node=run.current_node,
                base_commit_sha=run.base_commit_sha,
            )

        current_status, _, _, _ = _derive_status_from_snapshot(current_snapshot)
        if current_status in _TERMINAL_STATUSES:
            raise ConflictError(
                f"Workflow in terminal status '{current_status}' cannot be cancelled.",
                details={"workflow_id": workflow_id, "status": current_status},
            )

        cancel_msg = reason or "Cancelled by operator."
        self._graph.update_state(
            config,
            {
                "status": "cancelled",
                "cancelled_by": actor,
                "cancel_reason": cancel_msg,
                "error_code": "WORKFLOW_CANCELLED",
                "error_message": cancel_msg,
            },
        )
        post_snapshot = self._graph.get_state(config)
        response = self._sync_projection(run=run, checkpoint_snapshot=post_snapshot)

        self._audit.record(
            actor=actor,
            action="workflow.cancel",
            resource_type="workflow",
            resource_id=run.id,
            details={"reason": cancel_msg, "previous_status": current_status},
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