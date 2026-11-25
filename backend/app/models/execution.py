"""ORM models for persisted code changes, sandbox tests, static analysis, and code reviews."""

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CodeChangeModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted validated cumulative code change snapshot for a workflow iteration."""

    __tablename__ = "code_changes"
    __table_args__ = (
        UniqueConstraint("workflow_id", "revision_index", name="uq_code_changes_wf_rev"),
        Index("ix_code_changes_workflow_hash", "workflow_id", "patch_hash"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    revision_index: Mapped[int] = mapped_column(Integer, nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    files_changed: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    lines_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lines_removed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_changed_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    protected_path_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    scope_drift_files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    test_integrity_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    unified_diff_preview: Mapped[str] = mapped_column(Text, default="", nullable=False)


class TestExecutionModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted isolated sandbox test run telemetry and redacted excerpts."""

    __tablename__ = "test_executions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "run_index", name="uq_test_exec_wf_run"),
        Index("ix_test_executions_workflow_passed", "workflow_id", "passed"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sandbox_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    install_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    network_policy: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    stdout_excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stderr_excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)


class StaticAnalysisFindingModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Normalized deterministic finding persisted from Ruff, Mypy, Bandit, or pip-audit."""

    __tablename__ = "static_analysis_findings"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "run_index",
            "finding_index",
            name="uq_static_finding_wf_run_idx",
        ),
        Index("ix_static_findings_wf_tool_sev", "workflow_id", "tool", "severity"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_index: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    rule_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), default="high", nullable=False)
    source_kind: Mapped[str] = mapped_column(
        String(32),
        default="deterministic_tool",
        nullable=False,
    )


class CodeReviewModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted structured AI + deterministic code review disposition."""

    __tablename__ = "code_reviews"
    __table_args__ = (
        UniqueConstraint("workflow_id", "review_index", name="uq_code_reviews_wf_idx"),
        Index("ix_code_reviews_workflow_passed", "workflow_id", "passed"),
    )

    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    review_index: Mapped[int] = mapped_column(Integer, nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    patch_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    test_integrity_assessments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    blocking_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)