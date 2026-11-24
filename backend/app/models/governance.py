"""ORM models for persisted workflow governance, tasks, approvals, and LLM usage."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentExecutionModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted step-by-step execution history for each LangGraph workflow node."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "step_index", name="uq_agent_exec_workflow_step"),
        Index("ix_agent_executions_workflow_node", "workflow_id", "node_name"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_name: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class TaskModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted implementation plan task associated with a workflow run."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("workflow_id", "task_key", name="uq_tasks_workflow_key"),
        Index("ix_tasks_workflow_status", "workflow_id", "status"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    task_key: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    expected_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    verification_steps: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    risk_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)


class TaskDependencyModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Directed dependency edge between two persisted workflow tasks."""

    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dep_edge"),
        Index("ix_task_dependencies_workflow", "workflow_id"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    depends_on_task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )


class ApprovalModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Immutable human approval/rejection record bound to base SHA and artifact hash."""

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_workflow_type", "workflow_id", "approval_type"),
        Index("ix_approvals_workflow_hash", "workflow_id", "target_hash"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    approval_type: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    target_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_protected_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class LLMUsageRecordModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-call LLM token, latency, retry, redaction, and cost accounting record."""

    __tablename__ = "llm_usage_records"
    __table_args__ = (
        UniqueConstraint("workflow_id", "call_index", name="uq_llm_usage_workflow_call"),
        Index("ix_llm_usage_workflow_model", "workflow_id", "model"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_status: Mapped[str] = mapped_column(String(32), default="unavailable", nullable=False)
    latency_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    secrets_redacted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)