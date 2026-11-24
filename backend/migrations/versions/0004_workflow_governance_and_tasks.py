"""Add agent_executions, tasks, task_dependencies, approvals, and llm_usage_records tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_executions",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("node_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "step_index", name="uq_agent_exec_workflow_step"),
    )
    op.create_index(op.f("ix_agent_executions_id"), "agent_executions", ["id"], unique=False)
    op.create_index(
        op.f("ix_agent_executions_workflow_id"),
        "agent_executions",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_executions_node_name"),
        "agent_executions",
        ["node_name"],
        unique=False,
    )
    op.create_index(
        "ix_agent_executions_workflow_node",
        "agent_executions",
        ["workflow_id", "node_name"],
        unique=False,
    )

    op.create_table(
        "tasks",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("task_key", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expected_files", sa.JSON(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("verification_steps", sa.JSON(), nullable=False),
        sa.Column("risk_notes", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "task_key", name="uq_tasks_workflow_key"),
    )
    op.create_index(op.f("ix_tasks_id"), "tasks", ["id"], unique=False)
    op.create_index(op.f("ix_tasks_workflow_id"), "tasks", ["workflow_id"], unique=False)
    op.create_index("ix_tasks_workflow_status", "tasks", ["workflow_id", "status"], unique=False)

    op.create_table(
        "task_dependencies",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("depends_on_task_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dep_edge"),
    )
    op.create_index(op.f("ix_task_dependencies_id"), "task_dependencies", ["id"], unique=False)
    op.create_index(
        op.f("ix_task_dependencies_task_id"),
        "task_dependencies",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_task_dependencies_depends_on_task_id"),
        "task_dependencies",
        ["depends_on_task_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_dependencies_workflow",
        "task_dependencies",
        ["workflow_id"],
        unique=False,
    )

    op.create_table(
        "approvals",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("approval_type", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=True),
        sa.Column("approved_protected_paths", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approvals_id"), "approvals", ["id"], unique=False)
    op.create_index(op.f("ix_approvals_workflow_id"), "approvals", ["workflow_id"], unique=False)
    op.create_index(
        op.f("ix_approvals_base_commit_sha"),
        "approvals",
        ["base_commit_sha"],
        unique=False,
    )
    op.create_index(
        "ix_approvals_workflow_type",
        "approvals",
        ["workflow_id", "approval_type"],
        unique=False,
    )
    op.create_index(
        "ix_approvals_workflow_hash",
        "approvals",
        ["workflow_id", "target_hash"],
        unique=False,
    )

    op.create_table(
        "llm_usage_records",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("cost_status", sa.String(length=32), nullable=False),
        sa.Column("latency_seconds", sa.Float(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("repair_attempts", sa.Integer(), nullable=False),
        sa.Column("secrets_redacted", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "call_index", name="uq_llm_usage_workflow_call"),
    )
    op.create_index(op.f("ix_llm_usage_records_id"), "llm_usage_records", ["id"], unique=False)
    op.create_index(
        op.f("ix_llm_usage_records_workflow_id"),
        "llm_usage_records",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_usage_workflow_model",
        "llm_usage_records",
        ["workflow_id", "model"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_llm_usage_workflow_model", table_name="llm_usage_records")
    op.drop_index(op.f("ix_llm_usage_records_workflow_id"), table_name="llm_usage_records")
    op.drop_index(op.f("ix_llm_usage_records_id"), table_name="llm_usage_records")
    op.drop_table("llm_usage_records")

    op.drop_index("ix_approvals_workflow_hash", table_name="approvals")
    op.drop_index("ix_approvals_workflow_type", table_name="approvals")
    op.drop_index(op.f("ix_approvals_base_commit_sha"), table_name="approvals")
    op.drop_index(op.f("ix_approvals_workflow_id"), table_name="approvals")
    op.drop_index(op.f("ix_approvals_id"), table_name="approvals")
    op.drop_table("approvals")

    op.drop_index("ix_task_dependencies_workflow", table_name="task_dependencies")
    op.drop_index(op.f("ix_task_dependencies_depends_on_task_id"), table_name="task_dependencies")
    op.drop_index(op.f("ix_task_dependencies_task_id"), table_name="task_dependencies")
    op.drop_index(op.f("ix_task_dependencies_id"), table_name="task_dependencies")
    op.drop_table("task_dependencies")

    op.drop_index("ix_tasks_workflow_status", table_name="tasks")
    op.drop_index(op.f("ix_tasks_workflow_id"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_id"), table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("ix_agent_executions_workflow_node", table_name="agent_executions")
    op.drop_index(op.f("ix_agent_executions_node_name"), table_name="agent_executions")
    op.drop_index(op.f("ix_agent_executions_workflow_id"), table_name="agent_executions")
    op.drop_index(op.f("ix_agent_executions_id"), table_name="agent_executions")
    op.drop_table("agent_executions")