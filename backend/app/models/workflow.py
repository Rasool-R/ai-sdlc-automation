"""ORM model for the application-level workflow execution projection."""

from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkflowRunModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Read-model projection of a workflow run owned by LangGraph checkpoints."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_repo_status", "repository_id", "status"),
    )

    repository_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    base_branch: Mapped[str] = mapped_column(String(200), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(48), default="pending", index=True, nullable=False)
    current_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_limit_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)