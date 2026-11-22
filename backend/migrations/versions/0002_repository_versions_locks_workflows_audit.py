"""Add repository metadata columns, versions, locks, workflow projections, and audit logs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("full_name", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "repositories",
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="github"),
    )
    op.add_column(
        "repositories",
        sa.Column("github_installation_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "repositories",
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index(op.f("ix_repositories_full_name"), "repositories", ["full_name"], unique=False)

    op.create_table(
        "repository_versions",
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("branch_name", sa.String(length=200), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("commit_message", sa.Text(), nullable=True),
        sa.Column("author_name", sa.String(length=200), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_id",
            "branch_name",
            "commit_sha",
            name="uq_repo_version_branch_sha",
        ),
    )
    op.create_index(
        op.f("ix_repository_versions_id"),
        "repository_versions",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_repository_versions_repository_id"),
        "repository_versions",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_repository_versions_commit_sha"),
        "repository_versions",
        ["commit_sha"],
        unique=False,
    )
    op.create_index(
        "ix_repo_versions_repo_sha",
        "repository_versions",
        ["repository_id", "commit_sha"],
        unique=False,
    )

    op.create_table(
        "repository_locks",
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_repository_locks_id"), "repository_locks", ["id"], unique=False)
    op.create_index(
        op.f("ix_repository_locks_repository_id"),
        "repository_locks",
        ["repository_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_repository_locks_expires_at"),
        "repository_locks",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "workflow_runs",
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("base_branch", sa.String(length=200), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("current_node", sa.String(length=64), nullable=True),
        sa.Column("cost_limit_usd", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workflow_runs_id"), "workflow_runs", ["id"], unique=False)
    op.create_index(
        op.f("ix_workflow_runs_repository_id"),
        "workflow_runs",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_runs_base_commit_sha"),
        "workflow_runs",
        ["base_commit_sha"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_runs_thread_id"),
        "workflow_runs",
        ["thread_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_workflow_runs_idempotency_key"),
        "workflow_runs",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(op.f("ix_workflow_runs_status"), "workflow_runs", ["status"], unique=False)
    op.create_index(
        "ix_workflow_runs_repo_status",
        "workflow_runs",
        ["repository_id", "status"],
        unique=False,
    )

    op.create_table(
        "audit_logs",
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_id"), "audit_logs", ["id"], unique=False)
    op.create_index(op.f("ix_audit_logs_actor"), "audit_logs", ["actor"], unique=False)
    op.create_index(op.f("ix_audit_logs_action"), "audit_logs", ["action"], unique=False)
    op.create_index(
        "ix_audit_logs_resource",
        "audit_logs",
        ["resource_type", "resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_action_created",
        "audit_logs",
        ["action", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_action_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_action"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_actor"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_id"), table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_workflow_runs_repo_status", table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_status"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_idempotency_key"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_thread_id"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_base_commit_sha"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_repository_id"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_id"), table_name="workflow_runs")
    op.drop_table("workflow_runs")

    op.drop_index(op.f("ix_repository_locks_expires_at"), table_name="repository_locks")
    op.drop_index(op.f("ix_repository_locks_repository_id"), table_name="repository_locks")
    op.drop_index(op.f("ix_repository_locks_id"), table_name="repository_locks")
    op.drop_table("repository_locks")

    op.drop_index("ix_repo_versions_repo_sha", table_name="repository_versions")
    op.drop_index(op.f("ix_repository_versions_commit_sha"), table_name="repository_versions")
    op.drop_index(op.f("ix_repository_versions_repository_id"), table_name="repository_versions")
    op.drop_index(op.f("ix_repository_versions_id"), table_name="repository_versions")
    op.drop_table("repository_versions")

    op.drop_index(op.f("ix_repositories_full_name"), table_name="repositories")
    op.drop_column("repositories", "is_private")
    op.drop_column("repositories", "github_installation_id")
    op.drop_column("repositories", "provider")
    op.drop_column("repositories", "full_name")