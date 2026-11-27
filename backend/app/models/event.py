"""ORM model for persisted workflow progress events with monotonic IDs."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_MonotonicEventIdType = BigInteger().with_variant(Integer, "sqlite")


class WorkflowEventModel(Base):
    """Append-only workflow event record ordered strictly by a monotonic integer ID."""

    __tablename__ = "workflow_events"
    __table_args__ = (
        Index("ix_workflow_events_workflow_id_id", "workflow_id", "id"),
        Index("ix_workflow_events_workflow_created", "workflow_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        _MonotonicEventIdType,
        primary_key=True,
        autoincrement=True,
    )
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    node_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
        nullable=False,
    )