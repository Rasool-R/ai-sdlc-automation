"""ORM models for connected repositories, immutable base versions, and workspace locks."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RepositoryModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Database model representing a connected remote code repository."""

    __tablename__ = "repositories"

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="github", nullable=False)
    clone_url: Mapped[str] = mapped_column(String(500), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(100), default="main", nullable=False)
    github_installation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_data_sharing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RepositoryVersionModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Immutable snapshot binding a repository branch to an exact base commit SHA."""

    __tablename__ = "repository_versions"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "branch_name",
            "commit_sha",
            name="uq_repo_version_branch_sha",
        ),
        Index("ix_repo_versions_repo_sha", "repository_id", "commit_sha"),
    )

    repository_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    branch_name: Mapped[str] = mapped_column(String(200), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RepositoryLockModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Exclusive concurrency lock preventing overlapping mutations on a repository."""

    __tablename__ = "repository_locks"

    repository_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )