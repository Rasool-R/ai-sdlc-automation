"""Exclusive repository lock service preventing concurrent workspace conflicts."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.models.repository import RepositoryLockModel


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime value to timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class RepositoryLockService:
    """Manages expiring exclusive locks per repository to enforce concurrency safety."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def acquire(
        self,
        *,
        repository_id: str,
        owner_id: str,
        operation: str,
        ttl_seconds: int = 900,
    ) -> RepositoryLockModel:
        """Acquire an exclusive lock or reclaim an expired lock for a repository."""
        now = datetime.now(UTC)
        new_expiry = now + timedelta(seconds=ttl_seconds)

        existing = self._db.scalar(
            select(RepositoryLockModel).where(RepositoryLockModel.repository_id == repository_id)
        )

        if existing is not None:
            if _as_utc(existing.expires_at) > now and existing.owner_id != owner_id:
                raise ConflictError(
                    "Repository is currently locked by another active operation.",
                    details={
                        "repository_id": repository_id,
                        "locked_by": existing.owner_id,
                        "operation": existing.operation,
                    },
                )
            existing.owner_id = owner_id
            existing.operation = operation
            existing.expires_at = new_expiry
            self._db.flush()
            return existing

        lock = RepositoryLockModel(
            repository_id=repository_id,
            owner_id=owner_id,
            operation=operation,
            expires_at=new_expiry,
        )
        self._db.add(lock)
        try:
            self._db.flush()
        except IntegrityError as exc:
            self._db.rollback()
            raise ConflictError(
                "Repository lock contention detected during acquisition.",
                details={"repository_id": repository_id},
            ) from exc
        return lock

    def release(self, *, repository_id: str, owner_id: str) -> bool:
        """Release a repository lock only if held by the specified owner."""
        existing = self._db.scalar(
            select(RepositoryLockModel).where(RepositoryLockModel.repository_id == repository_id)
        )
        if existing is None:
            return False
        if existing.owner_id != owner_id:
            raise ConflictError(
                "Cannot release lock owned by another operation.",
                details={
                    "repository_id": repository_id,
                    "locked_by": existing.owner_id,
                },
            )
        self._db.delete(existing)
        self._db.flush()
        return True

    def is_locked(self, *, repository_id: str) -> bool:
        """Return True when an unexpired lock currently exists for the repository."""
        existing = self._db.scalar(
            select(RepositoryLockModel).where(RepositoryLockModel.repository_id == repository_id)
        )
        if existing is None:
            return False
        return _as_utc(existing.expires_at) > datetime.now(UTC)