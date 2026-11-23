"""Controlled patch validation, test-integrity verification, and sandbox execution."""

from app.execution.patch_service import PatchService, classify_protected_path
from app.execution.sandbox_client import (
    SandboxClient,
    SandboxExecutionResult,
    SandboxRunReport,
    package_workspace_archive,
)
from app.execution.schemas import (
    PATCH_SCHEMA_VERSION,
    FilePatchOperation,
    PatchApplyResult,
    PatchBundle,
    PatchValidationResult,
    ProtectedPathFlag,
    SearchReplaceHunk,
    TestIntegrityFlag,
)
from app.execution.test_integrity import inspect_test_integrity

__all__ = [
    "PATCH_SCHEMA_VERSION",
    "FilePatchOperation",
    "PatchApplyResult",
    "PatchBundle",
    "PatchService",
    "PatchValidationResult",
    "ProtectedPathFlag",
    "SandboxClient",
    "SandboxExecutionResult",
    "SandboxRunReport",
    "SearchReplaceHunk",
    "TestIntegrityFlag",
    "classify_protected_path",
    "inspect_test_integrity",
    "package_workspace_archive",
]