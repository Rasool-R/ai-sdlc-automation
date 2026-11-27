"""Add pull_requests and workflow_artifacts tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pull_requests",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("head_branch", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=200), nullable=False),
        sa.Column("base_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("head_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("html_url", sa.String(length=1024), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("ci_status", sa.String(length=32), nullable=False),
        sa.Column("review_decision", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
        sa.UniqueConstraint("repository_id", "pr_number", name="uq_pr_repo_number"),
    )
    op.create_index(op.f("ix_pull_requests_id"), "pull_requests", ["id"], unique=False)
    op.create_index(
        op.f("ix_pull_requests_workflow_id"), "pull_requests", ["workflow_id"], unique=False
    )
    op.create_index(
        op.f("ix_pull_requests_repository_id"), "pull_requests", ["repository_id"], unique=False
    )
    op.create_index(
        op.f("ix_pull_requests_operation_id"), "pull_requests", ["operation_id"], unique=True
    )
    op.create_index(
        op.f("ix_pull_requests_base_commit_sha"),
        "pull_requests",
        ["base_commit_sha"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pull_requests_patch_hash"), "pull_requests", ["patch_hash"], unique=False
    )
    op.create_index(op.f("ix_pull_requests_state"), "pull_requests", ["state"], unique=False)
    op.create_index(
        "ix_pull_requests_workflow_state",
        "pull_requests",
        ["workflow_id", "state"],
        unique=False,
    )

    op.create_table(
        "workflow_artifacts",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
        sa.UniqueConstraint(
            "workflow_id",
            "artifact_type",
            "version",
            name="uq_workflow_artifact_version",
        ),
    )
    op.create_index(op.f("ix_workflow_artifacts_id"), "workflow_artifacts", ["id"], unique=False)
    op.create_index(
        op.f("ix_workflow_artifacts_workflow_id"),
        "workflow_artifacts",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_artifacts_artifact_type"),
        "workflow_artifacts",
        ["artifact_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_artifacts_created_by"),
        "workflow_artifacts",
        ["created_by"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_artifacts_expires_at"),
        "workflow_artifacts",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_artifacts_workflow_type",
        "workflow_artifacts",
        ["workflow_id", "artifact_type"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_artifacts_owner",
        "workflow_artifacts",
        ["created_by", "workflow_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_artifacts_owner", table_name="workflow_artifacts")
    op.drop_index("ix_workflow_artifacts_workflow_type", table_name="workflow_artifacts")
    op.drop_index(op.f("ix_workflow_artifacts_expires_at"), table_name="workflow_artifacts")
    op.drop_index(op.f("ix_workflow_artifacts_created_by"), table_name="workflow_artifacts")
    op.drop_index(op.f("ix_workflow_artifacts_artifact_type"), table_name="workflow_artifacts")
    op.drop_index(op.f("ix_workflow_artifacts_workflow_id"), table_name="workflow_artifacts")
    op.drop_index(op.f("ix_workflow_artifacts_id"), table_name="workflow_artifacts")
    op.drop_table("workflow_artifacts")

    op.drop_index("ix_pull_requests_workflow_state", table_name="pull_requests")
    op.drop_index(op.f("ix_pull_requests_state"), table_name="pull_requests")
    op.drop_index(op.f("ix_pull_requests_patch_hash"), table_name="pull_requests")
    op.drop_index(op.f("ix_pull_requests_base_commit_sha"), table_name="pull_requests")
    op.drop_index(op.f("ix_pull_requests_operation_id"), table_name="pull_requests")
    op.drop_index(op.f("ix_pull_requests_repository_id"), table_name="pull_requests")
    op.drop_index(op.f("ix_pull_requests_workflow_id"), table_name="pull_requests")
    op.drop_index(op.f("ix_pull_requests_id"), table_name="pull_requests")
    op.drop_table("pull_requests")