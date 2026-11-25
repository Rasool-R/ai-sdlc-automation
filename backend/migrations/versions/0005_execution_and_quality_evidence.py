"""Add code_changes, test_executions, static_analysis_findings, and code_reviews tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "code_changes",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("revision_index", sa.Integer(), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("files_changed", sa.JSON(), nullable=False),
        sa.Column("lines_added", sa.Integer(), nullable=False),
        sa.Column("lines_removed", sa.Integer(), nullable=False),
        sa.Column("total_changed_lines", sa.Integer(), nullable=False),
        sa.Column("protected_path_flags", sa.JSON(), nullable=False),
        sa.Column("scope_drift_files", sa.JSON(), nullable=False),
        sa.Column("test_integrity_flags", sa.JSON(), nullable=False),
        sa.Column("unified_diff_preview", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "revision_index", name="uq_code_changes_wf_rev"),
    )
    op.create_index(op.f("ix_code_changes_id"), "code_changes", ["id"], unique=False)
    op.create_index(
        op.f("ix_code_changes_workflow_id"),
        "code_changes",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_changes_base_commit_sha"),
        "code_changes",
        ["base_commit_sha"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_changes_patch_hash"),
        "code_changes",
        ["patch_hash"],
        unique=False,
    )
    op.create_index(
        "ix_code_changes_workflow_hash",
        "code_changes",
        ["workflow_id", "patch_hash"],
        unique=False,
    )

    op.create_table(
        "test_executions",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("run_index", sa.Integer(), nullable=False),
        sa.Column("sandbox_run_id", sa.String(length=128), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("install_exit_code", sa.Integer(), nullable=True),
        sa.Column("test_exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column("network_policy", sa.String(length=32), nullable=False),
        sa.Column("stdout_excerpt", sa.Text(), nullable=False),
        sa.Column("stderr_excerpt", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "run_index", name="uq_test_exec_wf_run"),
    )
    op.create_index(op.f("ix_test_executions_id"), "test_executions", ["id"], unique=False)
    op.create_index(
        op.f("ix_test_executions_workflow_id"),
        "test_executions",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_test_executions_base_commit_sha"),
        "test_executions",
        ["base_commit_sha"],
        unique=False,
    )
    op.create_index(
        "ix_test_executions_workflow_passed",
        "test_executions",
        ["workflow_id", "passed"],
        unique=False,
    )

    op.create_table(
        "static_analysis_findings",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("run_index", sa.Integer(), nullable=False),
        sa.Column("finding_index", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column("rule_id", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id",
            "run_index",
            "finding_index",
            name="uq_static_finding_wf_run_idx",
        ),
    )
    op.create_index(
        op.f("ix_static_analysis_findings_id"),
        "static_analysis_findings",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_static_analysis_findings_workflow_id"),
        "static_analysis_findings",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_static_analysis_findings_tool"),
        "static_analysis_findings",
        ["tool"],
        unique=False,
    )
    op.create_index(
        op.f("ix_static_analysis_findings_rule_id"),
        "static_analysis_findings",
        ["rule_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_static_analysis_findings_severity"),
        "static_analysis_findings",
        ["severity"],
        unique=False,
    )
    op.create_index(
        "ix_static_findings_wf_tool_sev",
        "static_analysis_findings",
        ["workflow_id", "tool", "severity"],
        unique=False,
    )

    op.create_table(
        "code_reviews",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("review_index", sa.Integer(), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("test_integrity_assessments", sa.JSON(), nullable=False),
        sa.Column("blocking_reasons", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "review_index", name="uq_code_reviews_wf_idx"),
    )
    op.create_index(op.f("ix_code_reviews_id"), "code_reviews", ["id"], unique=False)
    op.create_index(
        op.f("ix_code_reviews_workflow_id"),
        "code_reviews",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_reviews_base_commit_sha"),
        "code_reviews",
        ["base_commit_sha"],
        unique=False,
    )
    op.create_index(
        "ix_code_reviews_workflow_passed",
        "code_reviews",
        ["workflow_id", "passed"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_code_reviews_workflow_passed", table_name="code_reviews")
    op.drop_index(op.f("ix_code_reviews_base_commit_sha"), table_name="code_reviews")
    op.drop_index(op.f("ix_code_reviews_workflow_id"), table_name="code_reviews")
    op.drop_index(op.f("ix_code_reviews_id"), table_name="code_reviews")
    op.drop_table("code_reviews")

    op.drop_index("ix_static_findings_wf_tool_sev", table_name="static_analysis_findings")
    op.drop_index(
        op.f("ix_static_analysis_findings_severity"),
        table_name="static_analysis_findings",
    )
    op.drop_index(
        op.f("ix_static_analysis_findings_rule_id"),
        table_name="static_analysis_findings",
    )
    op.drop_index(op.f("ix_static_analysis_findings_tool"), table_name="static_analysis_findings")
    op.drop_index(
        op.f("ix_static_analysis_findings_workflow_id"),
        table_name="static_analysis_findings",
    )
    op.drop_index(op.f("ix_static_analysis_findings_id"), table_name="static_analysis_findings")
    op.drop_table("static_analysis_findings")

    op.drop_index("ix_test_executions_workflow_passed", table_name="test_executions")
    op.drop_index(op.f("ix_test_executions_base_commit_sha"), table_name="test_executions")
    op.drop_index(op.f("ix_test_executions_workflow_id"), table_name="test_executions")
    op.drop_index(op.f("ix_test_executions_id"), table_name="test_executions")
    op.drop_table("test_executions")

    op.drop_index("ix_code_changes_workflow_hash", table_name="code_changes")
    op.drop_index(op.f("ix_code_changes_patch_hash"), table_name="code_changes")
    op.drop_index(op.f("ix_code_changes_base_commit_sha"), table_name="code_changes")
    op.drop_index(op.f("ix_code_changes_workflow_id"), table_name="code_changes")
    op.drop_index(op.f("ix_code_changes_id"), table_name="code_changes")
    op.drop_table("code_changes")