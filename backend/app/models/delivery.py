"""ORM models for persisted Pull Requests and versioned workflow artifacts."""

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PullRequestModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted record of an idempotently created Pull Request and its CI/review status."""

    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "pr_number", name="uq_pr_repo_number"),
        Index("ix_pull_requests_workflow_state", "workflow_id", "state"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    repository_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    operation_id: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), default="github", nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    head_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(200), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    head_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    html_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="open", index=True, nullable=False)
    ci_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    review_decision: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)


class WorkflowArtifactModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Metadata reference for an externally stored, checksummed workflow artifact."""

    __tablename__ = "workflow_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "artifact_type",
            "version",
            name="uq_workflow_artifact_version",
        ),
        Index("ix_workflow_artifacts_workflow_type", "workflow_id", "artifact_type"),
        Index("ix_workflow_artifacts_owner", "created_by", "workflow_id"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(32), default="local", nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
        nullable=False,
    )