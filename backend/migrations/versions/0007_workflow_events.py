"""Add workflow_events table with monotonic event IDs for cursor queries and SSE."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    event_id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "workflow_events",
        sa.Column("id", event_id_type, autoincrement=True, nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("node_name", sa.String(length=100), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_events_workflow_id"),
        "workflow_events",
        ["workflow_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_events_event_type"),
        "workflow_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_events_created_at"),
        "workflow_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_events_workflow_id_id",
        "workflow_events",
        ["workflow_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_events_workflow_created",
        "workflow_events",
        ["workflow_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_events_workflow_created", table_name="workflow_events")
    op.drop_index("ix_workflow_events_workflow_id_id", table_name="workflow_events")
    op.drop_index(op.f("ix_workflow_events_created_at"), table_name="workflow_events")
    op.drop_index(op.f("ix_workflow_events_event_type"), table_name="workflow_events")
    op.drop_index(op.f("ix_workflow_events_workflow_id"), table_name="workflow_events")
    op.drop_table("workflow_events")