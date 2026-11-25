"""Idempotent projection recorder syncing governance and execution evidence tables."""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import redact_text
from app.models.execution import (
    CodeChangeModel,
    CodeReviewModel,
    StaticAnalysisFindingModel,
    TestExecutionModel,
)
from app.models.governance import (
    AgentExecutionModel,
    ApprovalModel,
    LLMUsageRecordModel,
    TaskDependencyModel,
    TaskModel,
)

MAX_DIFF_PREVIEW_CHARS = 64_000
MAX_LOG_EXCERPT_CHARS = 16_000


def _parse_iso_datetime(raw: Any) -> datetime:
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def _parse_optional_decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


class WorkflowGovernanceRecorder:
    """Projects checkpoint history, tasks, approvals, usage, and quality evidence idempotently."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def sync_from_state(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        """Synchronize all governance and quality-evidence projections from checkpoint state."""
        self._sync_agent_executions(workflow_id=workflow_id, state=state)
        self._sync_llm_usage(workflow_id=workflow_id, state=state)
        self._sync_tasks(workflow_id=workflow_id, state=state)
        self._sync_approvals(workflow_id=workflow_id, state=state)
        self._sync_code_changes(workflow_id=workflow_id, state=state)
        self._sync_test_executions(workflow_id=workflow_id, state=state)
        self._sync_static_analysis_findings(workflow_id=workflow_id, state=state)
        self._sync_code_reviews(workflow_id=workflow_id, state=state)
        self._db.flush()

    def _sync_agent_executions(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        raw_history = state.get("node_history")
        if not isinstance(raw_history, list) or not raw_history:
            return

        existing_steps = set(
            self._db.scalars(
                select(AgentExecutionModel.step_index).where(
                    AgentExecutionModel.workflow_id == workflow_id
                )
            ).all()
        )
        workflow_status = str(state.get("status", "running"))
        last_idx = len(raw_history) - 1

        for idx, raw_node in enumerate(raw_history):
            if idx in existing_steps:
                continue
            node_name = str(raw_node)
            step_status = "completed"
            details: dict[str, Any] = {}
            if idx == last_idx and workflow_status in {"failed", "rejected", "cancelled"}:
                step_status = workflow_status
                if state.get("error_code"):
                    details["error_code"] = str(state["error_code"])
            self._db.add(
                AgentExecutionModel(
                    workflow_id=workflow_id,
                    step_index=idx,
                    node_name=node_name,
                    status=step_status,
                    details=details,
                )
            )

    def _sync_llm_usage(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        raw_records = state.get("usage_records")
        if not isinstance(raw_records, list) or not raw_records:
            return

        existing_indices = set(
            self._db.scalars(
                select(LLMUsageRecordModel.call_index).where(
                    LLMUsageRecordModel.workflow_id == workflow_id
                )
            ).all()
        )

        for idx, item in enumerate(raw_records):
            if idx in existing_indices or not isinstance(item, dict):
                continue
            self._db.add(
                LLMUsageRecordModel(
                    workflow_id=workflow_id,
                    call_index=idx,
                    provider=str(item.get("provider", "unknown")),
                    model=str(item.get("model", "unknown")),
                    prompt_version=str(item.get("prompt_version", "unknown")),
                    input_tokens=(
                        int(item["input_tokens"])
                        if isinstance(item.get("input_tokens"), int)
                        else None
                    ),
                    output_tokens=(
                        int(item["output_tokens"])
                        if isinstance(item.get("output_tokens"), int)
                        else None
                    ),
                    total_tokens=(
                        int(item["total_tokens"])
                        if isinstance(item.get("total_tokens"), int)
                        else None
                    ),
                    estimated_cost_usd=_parse_optional_decimal(item.get("estimated_cost_usd")),
                    cost_status=str(item.get("cost_status", "unavailable")),
                    latency_seconds=float(item.get("latency_seconds", 0.0) or 0.0),
                    retry_count=int(item.get("retry_count", 0) or 0),
                    repair_attempts=int(item.get("repair_attempts", 0) or 0),
                    secrets_redacted=int(item.get("secrets_redacted", 0) or 0),
                )
            )

    def _derive_task_status(self, *, state: Mapping[str, Any], default_status: str) -> str:
        wf_status = str(state.get("status", "running"))
        if wf_status == "completed":
            return "completed"
        if wf_status == "failed" and state.get("plan_approved") is True:
            return "failed"
        if state.get("plan_approved") is True and wf_status in {
            "running",
            "waiting_commit_approval",
        }:
            return "in_progress"
        return default_status

    def _sync_tasks(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        raw_plan = state.get("implementation_plan")
        if not isinstance(raw_plan, dict):
            return
        raw_tasks = raw_plan.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return

        existing_rows = list(
            self._db.scalars(select(TaskModel).where(TaskModel.workflow_id == workflow_id)).all()
        )
        task_by_key: dict[str, TaskModel] = {row.task_key: row for row in existing_rows}

        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            task_key = str(item.get("task_id", "")).strip()
            if not task_key:
                continue
            derived_status = self._derive_task_status(
                state=state,
                default_status=str(item.get("status", "pending")),
            )
            existing = task_by_key.get(task_key)
            if existing is not None:
                existing.status = derived_status
                continue

            new_task = TaskModel(
                workflow_id=workflow_id,
                task_key=task_key,
                title=str(item.get("title", task_key)),
                description=str(item.get("description", "")),
                priority=str(item.get("priority", "medium")),
                status=derived_status,
                expected_files=[str(p) for p in item.get("expected_files", [])],
                acceptance_criteria=[str(c) for c in item.get("acceptance_criteria", [])],
                verification_steps=[str(v) for v in item.get("verification_steps", [])],
                risk_notes=str(item.get("risk_notes", "")),
            )
            self._db.add(new_task)
            task_by_key[task_key] = new_task

        self._db.flush()

        existing_edge_rows = self._db.execute(
            select(
                TaskDependencyModel.task_id,
                TaskDependencyModel.depends_on_task_id,
            ).where(TaskDependencyModel.workflow_id == workflow_id)
        ).all()
        existing_edges: set[tuple[str, str]] = {
            (str(row[0]), str(row[1])) for row in existing_edge_rows
        }

        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            task_key = str(item.get("task_id", "")).strip()
            current_task = task_by_key.get(task_key)
            if current_task is None:
                continue
            for dep_key in item.get("dependencies", []):
                dep_row = task_by_key.get(str(dep_key).strip())
                if dep_row is None or dep_row.id == current_task.id:
                    continue
                edge = (current_task.id, dep_row.id)
                if edge in existing_edges:
                    continue
                self._db.add(
                    TaskDependencyModel(
                        workflow_id=workflow_id,
                        task_id=current_task.id,
                        depends_on_task_id=dep_row.id,
                    )
                )
                existing_edges.add(edge)

    def _sync_approvals(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        candidates: list[tuple[str, Any, str]] = [
            ("plan", state.get("plan_approval_record"), "plan_hash"),
            ("commit", state.get("commit_approval_record"), "patch_hash"),
        ]
        for approval_type, raw_record, hash_field in candidates:
            if not isinstance(raw_record, dict):
                continue
            raw_action = str(raw_record.get("action", "")).strip().lower()
            decision = "approved" if raw_action == "approve" else "rejected"
            actor = str(raw_record.get("actor", "operator"))
            base_sha = str(raw_record.get("base_commit_sha") or state.get("base_commit_sha", ""))
            raw_hash = raw_record.get(hash_field)
            target_hash = str(raw_hash) if raw_hash else None
            protected_paths = [
                str(p) for p in raw_record.get("approved_protected_paths", [])
            ]
            reason = str(raw_record["reason"]) if raw_record.get("reason") else None
            decided_at = _parse_iso_datetime(raw_record.get("recorded_at"))

            existing = self._db.scalar(
                select(ApprovalModel).where(
                    ApprovalModel.workflow_id == workflow_id,
                    ApprovalModel.approval_type == approval_type,
                    ApprovalModel.decision == decision,
                    ApprovalModel.actor == actor,
                    ApprovalModel.base_commit_sha == base_sha,
                    ApprovalModel.target_hash == target_hash,
                )
            )
            if existing is not None:
                continue

            self._db.add(
                ApprovalModel(
                    workflow_id=workflow_id,
                    approval_type=approval_type,
                    decision=decision,
                    actor=actor,
                    base_commit_sha=base_sha,
                    target_hash=target_hash,
                    approved_protected_paths=protected_paths,
                    reason=reason,
                    decided_at=decided_at,
                )
            )

    def _sync_code_changes(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        raw_history = state.get("code_change_history")
        if not isinstance(raw_history, list) or not raw_history:
            return

        existing_revs = set(
            self._db.scalars(
                select(CodeChangeModel.revision_index).where(
                    CodeChangeModel.workflow_id == workflow_id
                )
            ).all()
        )
        base_sha = str(state.get("base_commit_sha", ""))

        for idx, item in enumerate(raw_history):
            if idx in existing_revs or not isinstance(item, dict):
                continue
            raw_diff = str(item.get("unified_diff", ""))
            self._db.add(
                CodeChangeModel(
                    workflow_id=workflow_id,
                    revision_index=idx,
                    base_commit_sha=str(item.get("base_commit_sha") or base_sha),
                    patch_hash=str(item.get("patch_hash") or ""),
                    files_changed=[str(p) for p in item.get("files_changed", [])],
                    lines_added=int(item.get("lines_added", 0) or 0),
                    lines_removed=int(item.get("lines_removed", 0) or 0),
                    total_changed_lines=int(item.get("total_changed_lines", 0) or 0),
                    protected_path_flags=[
                        f for f in item.get("protected_path_flags", []) if isinstance(f, dict)
                    ],
                    scope_drift_files=[str(p) for p in item.get("scope_drift_files", [])],
                    test_integrity_flags=[
                        f for f in item.get("test_integrity_flags", []) if isinstance(f, dict)
                    ],
                    unified_diff_preview=redact_text(raw_diff)[:MAX_DIFF_PREVIEW_CHARS],
                )
            )

    def _sync_test_executions(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        raw_history = state.get("test_execution_history")
        if not isinstance(raw_history, list) or not raw_history:
            return

        existing_runs = set(
            self._db.scalars(
                select(TestExecutionModel.run_index).where(
                    TestExecutionModel.workflow_id == workflow_id
                )
            ).all()
        )
        base_sha = str(state.get("base_commit_sha", ""))

        for idx, item in enumerate(raw_history):
            if idx in existing_runs or not isinstance(item, dict):
                continue
            install_res = item.get("install_result")
            test_res = item.get("test_result")
            install_code = (
                int(install_res["exit_code"])
                if isinstance(install_res, dict) and isinstance(install_res.get("exit_code"), int)
                else None
            )
            test_code = (
                int(test_res["exit_code"])
                if isinstance(test_res, dict) and isinstance(test_res.get("exit_code"), int)
                else None
            )
            primary = test_res if isinstance(test_res, dict) else install_res
            duration = (
                float(primary.get("duration_seconds", 0.0) or 0.0)
                if isinstance(primary, dict)
                else 0.0
            )
            timed_out = (
                bool(primary.get("timed_out", False))
                if isinstance(primary, dict)
                else False
            )
            net_policy = (
                str(primary.get("network_policy", "none"))
                if isinstance(primary, dict)
                else "none"
            )
            stdout = (
                redact_text(str(primary.get("stdout", "")))[:MAX_LOG_EXCERPT_CHARS]
                if isinstance(primary, dict)
                else ""
            )
            stderr = (
                redact_text(str(primary.get("stderr", "")))[:MAX_LOG_EXCERPT_CHARS]
                if isinstance(primary, dict)
                else ""
            )
            self._db.add(
                TestExecutionModel(
                    workflow_id=workflow_id,
                    run_index=idx,
                    sandbox_run_id=str(item.get("run_id") or f"run-{idx}"),
                    base_commit_sha=str(item.get("base_commit_sha") or base_sha),
                    passed=bool(item.get("passed", False)),
                    install_exit_code=install_code,
                    test_exit_code=test_code,
                    duration_seconds=duration,
                    timed_out=timed_out,
                    network_policy=net_policy,
                    stdout_excerpt=stdout,
                    stderr_excerpt=stderr,
                )
            )

    def _sync_static_analysis_findings(
        self,
        *,
        workflow_id: str,
        state: Mapping[str, Any],
    ) -> None:
        raw_history = state.get("static_analysis_history")
        if not isinstance(raw_history, list) or not raw_history:
            return

        existing_rows = self._db.execute(
            select(
                StaticAnalysisFindingModel.run_index,
                StaticAnalysisFindingModel.finding_index,
            ).where(StaticAnalysisFindingModel.workflow_id == workflow_id)
        ).all()
        existing_keys: set[tuple[int, int]] = {
            (int(row[0]), int(row[1])) for row in existing_rows
        }

        for run_idx, report in enumerate(raw_history):
            if not isinstance(report, dict):
                continue
            raw_findings = report.get("findings")
            if not isinstance(raw_findings, list):
                continue
            for find_idx, finding in enumerate(raw_findings):
                if (run_idx, find_idx) in existing_keys or not isinstance(finding, dict):
                    continue
                raw_start = finding.get("start_line")
                raw_end = finding.get("end_line")
                self._db.add(
                    StaticAnalysisFindingModel(
                        workflow_id=workflow_id,
                        run_index=run_idx,
                        finding_index=find_idx,
                        tool=str(finding.get("tool", "unknown")),
                        rule_id=str(finding.get("rule_id", "UNKNOWN")),
                        severity=str(finding.get("severity", "Low")),
                        file_path=str(finding.get("file_path", ".")),
                        start_line=int(raw_start) if isinstance(raw_start, int) else None,
                        end_line=int(raw_end) if isinstance(raw_end, int) else None,
                        message=redact_text(str(finding.get("message", "")))[:2000],
                        confidence=str(finding.get("confidence", "high")),
                        source_kind=str(finding.get("source_kind", "deterministic_tool")),
                    )
                )
                existing_keys.add((run_idx, find_idx))

    def _sync_code_reviews(self, *, workflow_id: str, state: Mapping[str, Any]) -> None:
        raw_history = state.get("code_review_history")
        if not isinstance(raw_history, list) or not raw_history:
            return

        existing_indices = set(
            self._db.scalars(
                select(CodeReviewModel.review_index).where(
                    CodeReviewModel.workflow_id == workflow_id
                )
            ).all()
        )
        base_sha = str(state.get("base_commit_sha", ""))

        for idx, item in enumerate(raw_history):
            if idx in existing_indices or not isinstance(item, dict):
                continue
            raw_patch_hash = item.get("patch_hash")
            self._db.add(
                CodeReviewModel(
                    workflow_id=workflow_id,
                    review_index=idx,
                    base_commit_sha=str(item.get("base_commit_sha") or base_sha),
                    patch_hash=str(raw_patch_hash) if raw_patch_hash else None,
                    passed=bool(item.get("passed", False)),
                    summary=redact_text(str(item.get("summary", ""))),
                    findings=[f for f in item.get("findings", []) if isinstance(f, dict)],
                    test_integrity_assessments=[
                        a
                        for a in item.get("test_integrity_assessments", [])
                        if isinstance(a, dict)
                    ],
                    blocking_reasons=[str(r) for r in item.get("blocking_reasons", [])],
                )
            )