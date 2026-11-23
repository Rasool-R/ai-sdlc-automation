"""Versioned Pydantic v2 contracts for structured patches and deterministic safety flags."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

PATCH_SCHEMA_VERSION = "1.0"

PatchOperationType = Literal["create", "modify", "delete"]
ProtectedPathCategory = Literal[
    "ci_workflow",
    "docker",
    "secret_or_env",
    "lockfile",
    "dependency_manifest",
]
TestIntegrityRuleId = Literal[
    "test_file_deleted",
    "test_removed",
    "assertions_reduced",
    "skip_marker_added",
    "test_config_modified",
]


class SearchReplaceHunk(BaseModel):
    """Exact search/replace edit block applied deterministically to a UTF-8 text file."""

    search: str = Field(min_length=1)
    replace: str
    expected_occurrences: int = Field(default=1, ge=1, le=20)


class FilePatchOperation(BaseModel):
    """Structured file-level patch operation bound to an expected base content hash."""

    path: str = Field(min_length=1, max_length=512)
    operation: PatchOperationType
    base_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    content: str | None = None
    hunks: list[SearchReplaceHunk] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "FilePatchOperation":
        if self.operation == "create":
            if self.content is None:
                raise ValueError("'content' is required when operation is 'create'.")
            if self.base_content_hash is not None:
                raise ValueError("'base_content_hash' must be omitted when operation is 'create'.")
            if self.hunks:
                raise ValueError("'hunks' must be empty when operation is 'create'.")
        elif self.operation == "modify":
            if self.base_content_hash is None:
                raise ValueError("'base_content_hash' is required when operation is 'modify'.")
            if not self.hunks:
                raise ValueError("At least one hunk is required when operation is 'modify'.")
            if self.content is not None:
                raise ValueError("'content' must be omitted when operation is 'modify'.")
        elif self.operation == "delete":
            if self.base_content_hash is None:
                raise ValueError("'base_content_hash' is required when operation is 'delete'.")
            if self.content is not None or self.hunks:
                raise ValueError("content and hunks must be omitted when operation is 'delete'.")
        return self


class PatchBundle(BaseModel):
    """Atomic, version-pinned collection of structured file operations."""

    schema_version: str = PATCH_SCHEMA_VERSION
    base_commit_sha: str = Field(min_length=7, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)
    operations: list[FilePatchOperation] = Field(min_length=1)


class ProtectedPathFlag(BaseModel):
    """Flag raised when a patch touches a security-sensitive or configuration path."""

    file_path: str
    category: ProtectedPathCategory
    reason: str
    declared_in_plan: bool = False
    approved: bool = False


class TestIntegrityFlag(BaseModel):
    """Deterministic flag raised when a patch removes, skips, or weakens tests."""

    file_path: str
    rule_id: TestIntegrityRuleId
    severity: Literal["high", "medium"]
    message: str
    symbols: list[str] = Field(default_factory=list)


class PatchValidationResult(BaseModel):
    """Complete deterministic validation report and unified diff generated before applying."""

    valid: bool
    base_commit_sha: str
    patch_hash: str
    unified_diff: str
    files_changed: list[str] = Field(default_factory=list)
    lines_added: int = Field(default=0, ge=0)
    lines_removed: int = Field(default=0, ge=0)
    total_changed_lines: int = Field(default=0, ge=0)
    protected_path_flags: list[ProtectedPathFlag] = Field(default_factory=list)
    scope_drift_files: list[str] = Field(default_factory=list)
    test_integrity_flags: list[TestIntegrityFlag] = Field(default_factory=list)
    requires_human_approval: bool = False
    errors: list[str] = Field(default_factory=list)


class PatchApplyResult(BaseModel):
    """Outcome of atomically applying a validated PatchBundle to an isolated workspace."""

    applied: bool
    base_commit_sha: str
    patch_hash: str
    unified_diff: str
    files_changed: list[str]
    lines_added: int = Field(ge=0)
    lines_removed: int = Field(ge=0)
    total_changed_lines: int = Field(ge=0)
    protected_path_flags: list[ProtectedPathFlag] = Field(default_factory=list)
    scope_drift_files: list[str] = Field(default_factory=list)
    test_integrity_flags: list[TestIntegrityFlag] = Field(default_factory=list)