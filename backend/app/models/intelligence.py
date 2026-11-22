"""ORM models for versioned repository index snapshots, code symbols, and relationships."""

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RepositoryIndexSnapshotModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted index snapshot binding extracted metadata to an exact commit SHA."""

    __tablename__ = "repository_index_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "commit_sha",
            name="uq_repo_index_snapshot_repo_sha",
        ),
        Index("ix_repo_index_snapshots_repo_created", "repository_id", "created_at"),
    )

    repository_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    file_hashes: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    files_indexed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    symbols_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    relationships_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parse_errors_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    semantic_indexed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CodeSymbolModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted structural code unit (module, class, function, method) with provenance."""

    __tablename__ = "code_symbols"
    __table_args__ = (
        Index("ix_code_symbols_repo_sha_path", "repository_id", "commit_sha", "file_path"),
        Index("ix_code_symbols_repo_sha_name", "repository_id", "commit_sha", "name"),
        Index("ix_code_symbols_repo_sha_qname", "repository_id", "commit_sha", "qualified_name"),
    )

    repository_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repository_index_snapshots.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    symbol_id: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    qualified_name: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    docstring: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CodeRelationshipModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Persisted directed structural or dependency edge between indexed symbols."""

    __tablename__ = "code_relationships"
    __table_args__ = (
        Index(
            "ix_code_relationships_repo_sha_source",
            "repository_id",
            "commit_sha",
            "source_symbol",
        ),
        Index(
            "ix_code_relationships_repo_sha_target",
            "repository_id",
            "commit_sha",
            "target_symbol",
        ),
        Index(
            "ix_code_relationships_repo_sha_path",
            "repository_id",
            "commit_sha",
            "file_path",
        ),
    )

    repository_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repository_index_snapshots.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    commit_sha: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_symbol: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    target_symbol: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)