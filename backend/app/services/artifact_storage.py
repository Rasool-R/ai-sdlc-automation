"""Pluggable ArtifactStorage abstraction, local filesystem adapter, and persistence service."""

import contextlib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppError,
    AuthorizationError,
    ConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from app.core.logging import redact_data, redact_text
from app.models.delivery import WorkflowArtifactModel

_SAFE_STORAGE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
DEFAULT_RETENTION_DAYS = 30
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class StoredArtifactBlob:
    """Integrity metadata returned after writing an artifact payload to storage."""

    storage_backend: str
    storage_key: str
    checksum_sha256: str
    size_bytes: int


class ArtifactStorage(Protocol):
    """Storage interface decoupling workflow artifacts from local disk or S3 backends."""

    @property
    def backend_name(self) -> str:
        """Identifier of the storage backend (e.g., 'local', 's3')."""

    def put(self, storage_key: str, data: bytes) -> StoredArtifactBlob:
        """Persist bytes atomically under storage_key and return checksum metadata."""

    def get(self, storage_key: str, *, expected_sha256: str | None = None) -> bytes:
        """Read bytes for storage_key and verify SHA-256 integrity when provided."""

    def delete(self, storage_key: str) -> bool:
        """Delete an object from storage; return True if removed."""

    def exists(self, storage_key: str) -> bool:
        """Return True if an object exists under storage_key."""


def _validate_storage_key(storage_key: str) -> str:
    cleaned = storage_key.replace("\\", "/").strip()
    if (
        not cleaned
        or cleaned.startswith("/")
        or ":" in cleaned.split("/", 1)[0]
        or not _SAFE_STORAGE_KEY_PATTERN.match(cleaned)
    ):
        raise ValidationFailedError(
            "Artifact storage key contains invalid characters or absolute path.",
            details={"storage_key": storage_key},
        )
    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValidationFailedError(
            "Artifact storage key cannot contain traversal segments.",
            details={"storage_key": storage_key},
        )
    return "/".join(parts)


class LocalArtifactStorage:
    """Confined local filesystem adapter with atomic writes and SHA-256 integrity checks."""

    def __init__(self, *, root_dir: Path | None = None, settings: Settings | None = None) -> None:
        resolved_settings = settings or get_settings()
        base_path = root_dir if root_dir is not None else resolved_settings.ARTIFACT_STORAGE_PATH
        self._root = base_path.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def backend_name(self) -> str:
        return "local"

    @property
    def root_dir(self) -> Path:
        return self._root

    def _resolve_target(self, storage_key: str) -> tuple[str, Path]:
        normalized = _validate_storage_key(storage_key)
        current = self._root
        for part in Path(normalized).parts:
            current = current / part
            if current.is_symlink():
                raise ValidationFailedError(
                    "Artifact storage path traverses a symbolic link.",
                    details={"storage_key": normalized},
                )
        resolved = current.resolve()
        if not resolved.is_relative_to(self._root) or resolved == self._root:
            raise ValidationFailedError(
                "Resolved artifact path escapes configured storage root.",
                details={"storage_key": normalized},
            )
        return normalized, resolved

    def put(self, storage_key: str, data: bytes) -> StoredArtifactBlob:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValidationFailedError(
                f"Artifact size ({len(data)} bytes) exceeds limit ({MAX_ARTIFACT_BYTES} bytes)."
            )
        normalized, target = self._resolve_target(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256(data).hexdigest()

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=".tmp_art_",
                delete=False,
            ) as tmp_fh:
                tmp_fh.write(data)
                tmp_fh.flush()
                tmp_path = Path(tmp_fh.name)
            os.replace(tmp_path, target)
            tmp_path = None
        except OSError as exc:
            raise AppError(
                "Failed to persist artifact to local storage.",
                code="ARTIFACT_STORAGE_ERROR",
                status_code=500,
                details={"storage_key": normalized, "reason": type(exc).__name__},
            ) from exc
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

        return StoredArtifactBlob(
            storage_backend=self.backend_name,
            storage_key=normalized,
            checksum_sha256=checksum,
            size_bytes=len(data),
        )

    def get(self, storage_key: str, *, expected_sha256: str | None = None) -> bytes:
        normalized, target = self._resolve_target(storage_key)
        if not target.is_file():
            raise ResourceNotFoundError(
                "Artifact blob not found in storage.",
                details={"storage_key": normalized},
            )
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise AppError(
                "Failed to read artifact blob from storage.",
                code="ARTIFACT_STORAGE_ERROR",
                status_code=500,
                details={"storage_key": normalized, "reason": type(exc).__name__},
            ) from exc

        if expected_sha256 is not None:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected_sha256:
                raise ConflictError(
                    "Artifact integrity verification failed: SHA-256 checksum mismatch.",
                    details={
                        "storage_key": normalized,
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual,
                    },
                )
        return raw

    def delete(self, storage_key: str) -> bool:
        _normalized, target = self._resolve_target(storage_key)
        if not target.exists():
            return False
        with contextlib.suppress(OSError):
            target.unlink()
            return True
        return False

    def exists(self, storage_key: str) -> bool:
        _normalized, target = self._resolve_target(storage_key)
        return target.is_file()


class ArtifactService:
    """Manages versioned, secret-redacted, checksummed workflow artifacts and retention."""

    def __init__(
        self,
        db: Session,
        *,
        storage: ArtifactStorage | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._settings = settings or get_settings()
        self._storage: ArtifactStorage = storage or LocalArtifactStorage(settings=self._settings)

    def _sanitize_filename(self, name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
        if not cleaned or not _SAFE_FILENAME_PATTERN.match(cleaned):
            return "artifact.dat"
        return cleaned

    def store_text_artifact(
        self,
        *,
        workflow_id: str,
        artifact_type: str,
        name: str,
        text_content: str,
        content_type: str,
        created_by: str,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> WorkflowArtifactModel:
        """Redact secrets, version, persist to ArtifactStorage, and record DB metadata."""
        redacted_text = redact_text(text_content)
        payload_bytes = redacted_text.encode("utf-8")
        return self._store_versioned_bytes(
            workflow_id=workflow_id,
            artifact_type=artifact_type,
            name=name,
            data=payload_bytes,
            content_type=content_type,
            created_by=created_by,
            retention_days=retention_days,
        )

    def store_json_artifact(
        self,
        *,
        workflow_id: str,
        artifact_type: str,
        name: str,
        payload: dict[str, Any] | list[Any],
        created_by: str,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> WorkflowArtifactModel:
        """Recursively redact secrets from JSON payload and store as versioned artifact."""
        redacted_obj = redact_data(payload)
        serialized = json.dumps(redacted_obj, indent=2, sort_keys=True, default=str)
        return self._store_versioned_bytes(
            workflow_id=workflow_id,
            artifact_type=artifact_type,
            name=name,
            data=serialized.encode("utf-8"),
            content_type="application/json",
            created_by=created_by,
            retention_days=retention_days,
        )

    def _store_versioned_bytes(
        self,
        *,
        workflow_id: str,
        artifact_type: str,
        name: str,
        data: bytes,
        content_type: str,
        created_by: str,
        retention_days: int,
    ) -> WorkflowArtifactModel:
        if retention_days < 1 or retention_days > 3650:
            raise ValidationFailedError("retention_days must be between 1 and 3650.")

        checksum = hashlib.sha256(data).hexdigest()
        latest = self._db.scalar(
            select(WorkflowArtifactModel)
            .where(
                WorkflowArtifactModel.workflow_id == workflow_id,
                WorkflowArtifactModel.artifact_type == artifact_type,
            )
            .order_by(WorkflowArtifactModel.version.desc())
            .limit(1)
        )
        # Idempotent skip when latest version already stores identical bytes
        if (
            latest is not None
            and latest.checksum_sha256 == checksum
            and self._storage.exists(latest.storage_key)
        ):
            return latest

        next_version = 1 if latest is None else latest.version + 1
        safe_name = self._sanitize_filename(name)
        safe_type = self._sanitize_filename(artifact_type)
        storage_key = f"workflows/{workflow_id}/{safe_type}/v{next_version}_{safe_name}"

        blob = self._storage.put(storage_key, data)
        expires_at = datetime.now(UTC) + timedelta(days=retention_days)
        row = WorkflowArtifactModel(
            workflow_id=workflow_id,
            artifact_type=artifact_type,
            name=safe_name,
            version=next_version,
            content_type=content_type,
            storage_backend=blob.storage_backend,
            storage_key=blob.storage_key,
            checksum_sha256=blob.checksum_sha256,
            size_bytes=blob.size_bytes,
            created_by=created_by,
            retention_days=retention_days,
            expires_at=expires_at,
        )
        self._db.add(row)
        self._db.flush()
        return row

    def list_workflow_artifacts(self, workflow_id: str) -> list[WorkflowArtifactModel]:
        """List all persisted artifacts belonging to a workflow in version order."""
        return list(
            self._db.scalars(
                select(WorkflowArtifactModel)
                .where(WorkflowArtifactModel.workflow_id == workflow_id)
                .order_by(
                    WorkflowArtifactModel.artifact_type.asc(),
                    WorkflowArtifactModel.version.asc(),
                )
            ).all()
        )

    def get_artifact_metadata(
        self,
        artifact_id: str,
        *,
        actor: str | None = None,
    ) -> WorkflowArtifactModel:
        """Fetch artifact metadata and optionally verify owner authorization."""
        row = self._db.scalar(
            select(WorkflowArtifactModel).where(WorkflowArtifactModel.id == artifact_id)
        )
        if row is None:
            raise ResourceNotFoundError(
                "Artifact not found.",
                details={"artifact_id": artifact_id},
            )
        if actor is not None and row.created_by != actor:
            raise AuthorizationError(
                "Operator is not authorized to access this artifact.",
                details={"artifact_id": artifact_id},
            )
        return row

    def read_artifact_bytes(
        self,
        artifact_id: str,
        *,
        actor: str | None = None,
    ) -> tuple[WorkflowArtifactModel, bytes]:
        """Retrieve authorized artifact bytes after verifying SHA-256 integrity."""
        row = self.get_artifact_metadata(artifact_id, actor=actor)
        data = self._storage.get(row.storage_key, expected_sha256=row.checksum_sha256)
        return row, data

    def purge_expired_artifacts(self, *, now: datetime | None = None) -> int:
        """Delete expired artifacts from both backing storage and database."""
        cutoff = now or datetime.now(UTC)
        expired_rows = list(
            self._db.scalars(
                select(WorkflowArtifactModel).where(WorkflowArtifactModel.expires_at <= cutoff)
            ).all()
        )
        purged = 0
        for row in expired_rows:
            self._storage.delete(row.storage_key)
            self._db.delete(row)
            purged += 1
        if purged:
            self._db.flush()
        return purged

    def count_workflow_artifacts(self, workflow_id: str) -> int:
        """Return total artifact count recorded for a workflow."""
        return int(
            self._db.scalar(
                select(func.count())
                .select_from(WorkflowArtifactModel)
                .where(WorkflowArtifactModel.workflow_id == workflow_id)
            )
            or 0
        )