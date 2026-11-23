"""Versioned Pydantic v2 contracts for the narrow internal sandbox-runner API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SANDBOX_SCHEMA_VERSION = "1.0"

CommandTemplate = Literal[
    "install_deps",
    "pytest",
    "ruff_check",
    "mypy_check",
    "bandit_check",
]
ExecutionPhase = Literal["dependency_install", "isolated_execution"]
NetworkPolicy = Literal["restricted_egress", "none"]
RunStatus = Literal["created", "ready", "running", "completed", "failed", "destroyed"]


class CreateRunRequest(BaseModel):
    """Request to allocate an isolated sandbox run workspace."""

    workflow_id: str = Field(min_length=1, max_length=128)
    repository_id: str = Field(min_length=1, max_length=128)
    base_commit_sha: str = Field(min_length=7, max_length=64)
    image: str | None = Field(default=None, min_length=3, max_length=255)
    cpu_limit: float | None = Field(default=None, gt=0.1, le=16.0)
    memory_limit_mb: int | None = Field(default=None, ge=256, le=32768)
    pids_limit: int | None = Field(default=None, ge=32, le=4096)
    timeout_seconds: int | None = Field(default=None, ge=10, le=3600)


class CreateRunResponse(BaseModel):
    """Metadata returned upon allocating a new sandbox run."""

    schema_version: str = SANDBOX_SCHEMA_VERSION
    run_id: str
    status: RunStatus
    image: str
    created_at: datetime


class UploadWorkspaceResponse(BaseModel):
    """Summary returned after safely extracting a workspace archive."""

    schema_version: str = SANDBOX_SCHEMA_VERSION
    run_id: str
    status: RunStatus
    files_extracted: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class ExecuteCommandRequest(BaseModel):
    """Request to execute a platform-controlled command template inside the sandbox."""

    template: CommandTemplate
    target_paths: list[str] = Field(default_factory=list, max_length=50)
    pytest_args: list[str] = Field(default_factory=list, max_length=20)
    extra_pip_packages: list[str] = Field(default_factory=list, max_length=20)
    timeout_seconds: int | None = Field(default=None, ge=5, le=3600)


class CommandExecutionRecord(BaseModel):
    """Captured execution telemetry for a single sandboxed command run."""

    execution_id: str
    template: CommandTemplate
    phase: ExecutionPhase
    network_policy: NetworkPolicy
    command_argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0.0)
    timed_out: bool = False
    started_at: datetime
    finished_at: datetime


class RunResultsResponse(BaseModel):
    """Aggregated run status and captured command execution records."""

    schema_version: str = SANDBOX_SCHEMA_VERSION
    run_id: str
    workflow_id: str
    repository_id: str
    base_commit_sha: str
    status: RunStatus
    workspace_uploaded: bool
    executions: list[CommandExecutionRecord] = Field(default_factory=list)


class DestroyRunResponse(BaseModel):
    """Confirmation returned after cleaning up a sandbox run."""

    run_id: str
    destroyed: bool