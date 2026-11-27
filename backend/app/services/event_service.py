"""Secret-safe workflow event persistence, idempotent transition recording, and SSE formatting."""

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ResourceNotFoundError
from app.core.logging import redact_data, redact_text
from app.models.event import WorkflowEventModel
from app.models.workflow import WorkflowRunModel
from app.schemas.event import WorkflowEventListResponse, WorkflowEventRead

_BLOCKED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "raw_reasoning",
        "chain_of_thought",
        "internal_monologue",
        "system_prompt",
        "private_key",
        "access_token",
    }
)
_HUMAN_WAITING_STATUSES: frozenset[str] = frozenset(
    {"waiting_clarification", "waiting_plan_approval", "waiting_commit_approval"}
)
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "rejected", "cancelled"}
)


def _strip_blocked_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _strip_blocked_keys(v)
            for k, v in value.items()
            if str(k).lower() not in _BLOCKED_PAYLOAD_KEYS
        }
    if isinstance(value, list):
        return [_strip_blocked_keys(item) for item in value]
    return value


def sanitize_event_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Remove restricted reasoning keys and redact secrets before persisting or streaming."""
    if not payload:
        return {}
    stripped = _strip_blocked_keys(dict(payload))
    redacted = redact_data(stripped)
    return redacted if isinstance(redacted, dict) else {}


def format_sse_event(event: WorkflowEventRead) -> str:
    """Serialize a persisted event into a standard Server-Sent Events frame."""
    data_json = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data_json}\n\n"


def format_sse_comment(comment: str) -> str:
    """Format an SSE comment line for keepalive heartbeats or stream boundaries."""
    cleaned = comment.replace("\r", " ").replace("\n", " ").strip()
    return f": {cleaned}\n\n"


class WorkflowEventService:
    """Persists and queries monotonic workflow events with secret redaction."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def ensure_workflow_exists(self, workflow_id: str) -> WorkflowRunModel:
        """Verify that a workflow exists or raise ResourceNotFoundError."""
        run = self._db.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id)
        )
        if run is None:
            raise ResourceNotFoundError(
                "Workflow not found.",
                details={"workflow_id": workflow_id},
            )
        return run

    def get_workflow_status(self, workflow_id: str) -> str | None:
        """Fetch current projection status for a workflow run."""
        return self._db.scalar(
            select(WorkflowRunModel.status).where(WorkflowRunModel.id == workflow_id)
        )

    def record_event(
        self,
        *,
        workflow_id: str,
        event_type: str,
        status: str,
        message: str,
        node_name: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> WorkflowEventModel:
        """Append a single secret-redacted workflow event and flush to obtain its monotonic ID."""
        row = WorkflowEventModel(
            workflow_id=workflow_id,
            event_type=event_type.strip()[:64],
            status=status.strip()[:48],
            node_name=node_name.strip()[:100] if node_name else None,
            message=redact_text(message.strip())[:2000],
            payload=sanitize_event_payload(payload),
        )
        self._db.add(row)
        self._db.flush()
        return row

    def list_events(
        self,
        *,
        workflow_id: str,
        after_id: int | None = None,
        limit: int = 100,
    ) -> WorkflowEventListResponse:
        """Return cursor-paginated workflow events ordered strictly by monotonic ID."""
        self.ensure_workflow_exists(workflow_id)
        filters = [WorkflowEventModel.workflow_id == workflow_id]
        if after_id is not None:
            filters.append(WorkflowEventModel.id > after_id)

        total = int(
            self._db.scalar(
                select(func.count()).select_from(WorkflowEventModel).where(*filters)
            )
            or 0
        )
        rows = list(
            self._db.scalars(
                select(WorkflowEventModel)
                .where(*filters)
                .order_by(WorkflowEventModel.id.asc())
                .limit(limit + 1)
            ).all()
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [WorkflowEventRead.model_validate(row) for row in page_rows]
        next_cursor = items[-1].id if items else after_id
        return WorkflowEventListResponse(
            workflow_id=workflow_id,
            items=items,
            total=total,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _build_node_step_summary(
        self,
        *,
        node_name: str,
        step_index: int,
        state: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "step_index": step_index,
            "node_name": node_name,
        }
        message = f"Completed workflow node '{node_name}'."

        if node_name == "planning":
            plan_raw = state.get("implementation_plan")
            tasks = plan_raw.get("tasks", []) if isinstance(plan_raw, dict) else []
            payload["tasks_count"] = len(tasks) if isinstance(tasks, list) else 0
            payload["plan_hash"] = state.get("plan_hash")
            message = f"Generated implementation plan with {payload['tasks_count']} task(s)."
        elif node_name == "coding":
            patch_val = state.get("patch_validation")
            if isinstance(patch_val, dict):
                files = patch_val.get("files_changed", [])
                payload["files_changed"] = list(files) if isinstance(files, list) else []
                payload["patch_hash"] = patch_val.get("patch_hash")
                payload["total_changed_lines"] = int(patch_val.get("total_changed_lines", 0) or 0)
                message = (
                    f"Validated and applied patch touching {len(payload['files_changed'])} file(s)."
                )
        elif node_name == "sandbox_test":
            sbx = state.get("sandbox_report")
            if isinstance(sbx, dict):
                passed = bool(sbx.get("passed"))
                payload["passed"] = passed
                payload["sandbox_run_id"] = sbx.get("run_id")
                message = (
                    "Isolated sandbox tests passed."
                    if passed
                    else "Isolated sandbox tests failed."
                )
        elif node_name == "static_analysis":
            static_rep = state.get("static_analysis_report")
            if isinstance(static_rep, dict):
                passed = bool(static_rep.get("passed"))
                payload["passed"] = passed
                payload["blocking_findings_count"] = int(
                    static_rep.get("blocking_findings_count", 0) or 0
                )
                payload["total_findings_count"] = int(
                    static_rep.get("total_findings_count", 0) or 0
                )
                message = (
                    "Deterministic static analysis passed."
                    if passed
                    else "Deterministic static analysis reported blocking finding(s)."
                )
        elif node_name == "code_review":
            review = state.get("code_review_report")
            if isinstance(review, dict):
                passed = bool(review.get("passed"))
                findings = review.get("findings", [])
                payload["passed"] = passed
                payload["findings_count"] = len(findings) if isinstance(findings, list) else 0
                payload["blocking_reasons"] = list(review.get("blocking_reasons", []))
                message = (
                    "Code review passed quality and safety gates."
                    if passed
                    else "Code review requested bounded corrections."
                )
        elif node_name == "test_failure_analysis":
            payload["test_correction_loops"] = int(state.get("test_correction_loops", 0) or 0)
            payload["total_correction_loops"] = int(state.get("total_correction_loops", 0) or 0)
            message = (
                f"Diagnosed test failure (correction loop #{payload['test_correction_loops']})."
            )
        elif node_name == "release_preparation":
            pr_deliv = state.get("pull_request_delivery")
            if isinstance(pr_deliv, dict) and pr_deliv.get("pr_number"):
                payload["pr_number"] = pr_deliv.get("pr_number")
                payload["pr_url"] = pr_deliv.get("html_url")
                payload["head_branch"] = pr_deliv.get("head_branch")
                message = f"Prepared release and opened Pull Request #{payload['pr_number']}."
            else:
                message = "Prepared release metadata and final completion report."

        return message, payload

    def sync_transition_events(
        self,
        *,
        workflow_id: str,
        status: str,
        current_node: str | None,
        state: Mapping[str, Any],
        interrupt_payload: dict[str, Any] | None = None,
    ) -> list[WorkflowEventModel]:
        """Idempotently record node, human-interrupt, and terminal events from checkpoint state."""
        emitted: list[WorkflowEventModel] = []
        existing_rows = list(
            self._db.scalars(
                select(WorkflowEventModel)
                .where(WorkflowEventModel.workflow_id == workflow_id)
                .order_by(WorkflowEventModel.id.asc())
            ).all()
        )

        recorded_steps: set[int] = set()
        recorded_interrupt_keys: set[str] = set()
        recorded_terminal_types: set[str] = set()
        for row in existing_rows:
            if row.event_type == "node.executed":
                raw_idx = row.payload.get("step_index")
                if isinstance(raw_idx, int):
                    recorded_steps.add(raw_idx)
            elif row.event_type == "human.action_required":
                raw_key = row.payload.get("interrupt_key")
                if isinstance(raw_key, str):
                    recorded_interrupt_keys.add(raw_key)
            elif row.event_type.startswith("workflow."):
                recorded_terminal_types.add(row.event_type)

        raw_history = state.get("node_history")
        history_len = len(raw_history) if isinstance(raw_history, list) else 0
        if isinstance(raw_history, list):
            for idx, raw_node in enumerate(raw_history):
                if idx in recorded_steps:
                    continue
                node_name = str(raw_node)
                msg, step_payload = self._build_node_step_summary(
                    node_name=node_name,
                    step_index=idx,
                    state=state,
                )
                emitted.append(
                    self.record_event(
                        workflow_id=workflow_id,
                        event_type="node.executed",
                        status=status,
                        node_name=node_name,
                        message=msg,
                        payload=step_payload,
                    )
                )
                recorded_steps.add(idx)

        if status in _HUMAN_WAITING_STATUSES and isinstance(interrupt_payload, dict):
            target_hash = str(
                interrupt_payload.get("plan_hash")
                or interrupt_payload.get("patch_hash")
                or ""
            )
            interrupt_key = f"{status}:{history_len}:{target_hash}"
            if interrupt_key not in recorded_interrupt_keys:
                safe_intr_payload: dict[str, Any] = {
                    "interrupt_key": interrupt_key,
                    "interrupt_type": interrupt_payload.get("type"),
                    "base_commit_sha": interrupt_payload.get("base_commit_sha"),
                    "plan_hash": interrupt_payload.get("plan_hash"),
                    "patch_hash": interrupt_payload.get("patch_hash"),
                    "expected_files": interrupt_payload.get("expected_files"),
                    "files_changed": interrupt_payload.get("files_changed"),
                    "questions": interrupt_payload.get("questions"),
                    "estimated_cost_usd": interrupt_payload.get("estimated_cost_usd"),
                    "cost_status": interrupt_payload.get("cost_status"),
                }
                emitted.append(
                    self.record_event(
                        workflow_id=workflow_id,
                        event_type="human.action_required",
                        status=status,
                        node_name=current_node,
                        message=f"Workflow paused at '{status}' awaiting operator action.",
                        payload=safe_intr_payload,
                    )
                )

        if status in _TERMINAL_STATUSES:
            terminal_type = f"workflow.{status}"
            if terminal_type not in recorded_terminal_types:
                pr_deliv = state.get("pull_request_delivery")
                terminal_payload: dict[str, Any] = {
                    "base_commit_sha": state.get("base_commit_sha"),
                    "llm_calls_count": int(state.get("llm_calls_count", 0) or 0),
                    "total_tokens": int(state.get("total_tokens", 0) or 0),
                    "estimated_cost_usd": state.get("estimated_cost_usd"),
                    "cost_status": state.get("cost_status"),
                    "error_code": state.get("error_code"),
                    "error_message": state.get("error_message"),
                    "pr_number": (
                        pr_deliv.get("pr_number") if isinstance(pr_deliv, dict) else None
                    ),
                    "pr_url": (
                        pr_deliv.get("html_url") if isinstance(pr_deliv, dict) else None
                    ),
                }
                msg = (
                    str(state.get("error_message"))
                    if status in {"failed", "rejected", "cancelled"} and state.get("error_message")
                    else f"Workflow reached terminal status '{status}'."
                )
                emitted.append(
                    self.record_event(
                        workflow_id=workflow_id,
                        event_type=terminal_type,
                        status=status,
                        node_name=current_node,
                        message=msg,
                        payload=terminal_payload,
                    )
                )

        return emitted