"""Audit logging service persisting secret-redacted operational records."""

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import correlation_id_ctx, redact_data
from app.models.audit import AuditLogModel


class AuditService:
    """Service responsible for recording immutable, secret-safe audit events."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def record(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        status: str = "success",
        details: dict[str, Any] | None = None,
    ) -> AuditLogModel:
        """Persist a secret-redacted audit entry tied to the current correlation ID."""
        sanitized_details = redact_data(details or {})
        entry = AuditLogModel(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            correlation_id=correlation_id_ctx.get(),
            details=sanitized_details if isinstance(sanitized_details, dict) else {},
        )
        self._db.add(entry)
        self._db.flush()
        return entry