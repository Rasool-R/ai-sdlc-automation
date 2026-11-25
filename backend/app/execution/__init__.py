"""Controlled patch validation, test-integrity verification, sandbox, and static analysis."""

from app.execution.patch_service import PatchService, classify_protected_path
from app.execution.sandbox_client import (
    SandboxClient,
    SandboxExecutionResult,
    SandboxRunReport,
    SandboxStaticAnalysisRawReport,
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
from app.execution.static_analysis import (
    STATIC_ANALYSIS_SCHEMA_VERSION,
    NormalizedFinding,
    PipAuditRawOutcome,
    PipAuditRunner,
    StaticAnalysisReport,
    StaticAnalysisService,
    SubprocessPipAuditRunner,
    ToolExecutionSummary,
    parse_bandit_json,
    parse_mypy_output,
    parse_pip_audit_json,
    parse_ruff_json,
)
from app.execution.test_integrity import inspect_test_integrity

__all__ = [
    "PATCH_SCHEMA_VERSION",
    "STATIC_ANALYSIS_SCHEMA_VERSION",
    "FilePatchOperation",
    "NormalizedFinding",
    "PatchApplyResult",
    "PatchBundle",
    "PatchService",
    "PatchValidationResult",
    "PipAuditRawOutcome",
    "PipAuditRunner",
    "ProtectedPathFlag",
    "SandboxClient",
    "SandboxExecutionResult",
    "SandboxRunReport",
    "SandboxStaticAnalysisRawReport",
    "SearchReplaceHunk",
    "StaticAnalysisReport",
    "StaticAnalysisService",
    "SubprocessPipAuditRunner",
    "TestIntegrityFlag",
    "ToolExecutionSummary",
    "classify_protected_path",
    "inspect_test_integrity",
    "package_workspace_archive",
    "parse_bandit_json",
    "parse_mypy_output",
    "parse_pip_audit_json",
    "parse_ruff_json",
]