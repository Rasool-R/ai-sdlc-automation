"""Add repository index snapshots, code symbols, and code relationship tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_index_snapshots",
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("file_hashes", sa.JSON(), nullable=False),
        sa.Column("files_indexed_count", sa.Integer(), nullable=False),
        sa.Column("symbols_count", sa.Integer(), nullable=False),
        sa.Column("relationships_count", sa.Integer(), nullable=False),
        sa.Column("parse_errors_count", sa.Integer(), nullable=False),
        sa.Column("semantic_indexed", sa.Boolean(), nullable=False),
        sa.Column("metadata_summary", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_id",
            "commit_sha",
            name="uq_repo_index_snapshot_repo_sha",
        ),
    )
    op.create_index(
        op.f("ix_repository_index_snapshots_id"),
        "repository_index_snapshots",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_repository_index_snapshots_repository_id"),
        "repository_index_snapshots",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_repository_index_snapshots_commit_sha"),
        "repository_index_snapshots",
        ["commit_sha"],
        unique=False,
    )
    op.create_index(
        "ix_repo_index_snapshots_repo_created",
        "repository_index_snapshots",
        ["repository_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "code_symbols",
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("symbol_id", sa.String(length=512), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("qualified_name", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("docstring", sa.Text(), nullable=True),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_index_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_code_symbols_id"), "code_symbols", ["id"], unique=False)
    op.create_index(
        op.f("ix_code_symbols_repository_id"),
        "code_symbols",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_symbols_snapshot_id"),
        "code_symbols",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_symbols_commit_sha"),
        "code_symbols",
        ["commit_sha"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_symbols_symbol_id"),
        "code_symbols",
        ["symbol_id"],
        unique=False,
    )
    op.create_index(op.f("ix_code_symbols_name"), "code_symbols", ["name"], unique=False)
    op.create_index(
        op.f("ix_code_symbols_qualified_name"),
        "code_symbols",
        ["qualified_name"],
        unique=False,
    )
    op.create_index(op.f("ix_code_symbols_kind"), "code_symbols", ["kind"], unique=False)
    op.create_index(
        op.f("ix_code_symbols_file_path"),
        "code_symbols",
        ["file_path"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_symbols_content_hash"),
        "code_symbols",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        "ix_code_symbols_repo_sha_path",
        "code_symbols",
        ["repository_id", "commit_sha", "file_path"],
        unique=False,
    )
    op.create_index(
        "ix_code_symbols_repo_sha_name",
        "code_symbols",
        ["repository_id", "commit_sha", "name"],
        unique=False,
    )
    op.create_index(
        "ix_code_symbols_repo_sha_qname",
        "code_symbols",
        ["repository_id", "commit_sha", "qualified_name"],
        unique=False,
    )

    op.create_table(
        "code_relationships",
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("source_symbol", sa.String(length=512), nullable=False),
        sa.Column("target_symbol", sa.String(length=512), nullable=False),
        sa.Column("relationship_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_index_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_code_relationships_id"),
        "code_relationships",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_relationships_repository_id"),
        "code_relationships",
        ["repository_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_relationships_snapshot_id"),
        "code_relationships",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_relationships_commit_sha"),
        "code_relationships",
        ["commit_sha"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_relationships_source_symbol"),
        "code_relationships",
        ["source_symbol"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_relationships_target_symbol"),
        "code_relationships",
        ["target_symbol"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_relationships_relationship_type"),
        "code_relationships",
        ["relationship_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_relationships_file_path"),
        "code_relationships",
        ["file_path"],
        unique=False,
    )
    op.create_index(
        "ix_code_relationships_repo_sha_source",
        "code_relationships",
        ["repository_id", "commit_sha", "source_symbol"],
        unique=False,
    )
    op.create_index(
        "ix_code_relationships_repo_sha_target",
        "code_relationships",
        ["repository_id", "commit_sha", "target_symbol"],
        unique=False,
    )
    op.create_index(
        "ix_code_relationships_repo_sha_path",
        "code_relationships",
        ["repository_id", "commit_sha", "file_path"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_code_relationships_repo_sha_path", table_name="code_relationships")
    op.drop_index("ix_code_relationships_repo_sha_target", table_name="code_relationships")
    op.drop_index("ix_code_relationships_repo_sha_source", table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_file_path"), table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_relationship_type"), table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_target_symbol"), table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_source_symbol"), table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_commit_sha"), table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_snapshot_id"), table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_repository_id"), table_name="code_relationships")
    op.drop_index(op.f("ix_code_relationships_id"), table_name="code_relationships")
    op.drop_table("code_relationships")

    op.drop_index("ix_code_symbols_repo_sha_qname", table_name="code_symbols")
    op.drop_index("ix_code_symbols_repo_sha_name", table_name="code_symbols")
    op.drop_index("ix_code_symbols_repo_sha_path", table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_content_hash"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_file_path"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_kind"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_qualified_name"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_name"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_symbol_id"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_commit_sha"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_snapshot_id"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_repository_id"), table_name="code_symbols")
    op.drop_index(op.f("ix_code_symbols_id"), table_name="code_symbols")
    op.drop_table("code_symbols")

    op.drop_index(
        "ix_repo_index_snapshots_repo_created",
        table_name="repository_index_snapshots",
    )
    op.drop_index(
        op.f("ix_repository_index_snapshots_commit_sha"),
        table_name="repository_index_snapshots",
    )
    op.drop_index(
        op.f("ix_repository_index_snapshots_repository_id"),
        table_name="repository_index_snapshots",
    )
    op.drop_index(
        op.f("ix_repository_index_snapshots_id"),
        table_name="repository_index_snapshots",
    )
    op.drop_table("repository_index_snapshots")