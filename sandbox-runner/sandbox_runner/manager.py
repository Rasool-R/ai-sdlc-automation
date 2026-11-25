"""Sandbox run lifecycle manager handling safe archive extraction, execution, and cleanup."""

import contextlib
import io
import os
import re
import shutil
import stat
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sandbox_runner.config import SandboxRunnerSettings
from sandbox_runner.runtime import (
    ContainerExecutionSpec,
    ContainerRuntime,
    build_template_command,
    validate_image_name,
)
from sandbox_runner.schemas import (
    CommandExecutionRecord,
    CreateRunRequest,
    CreateRunResponse,
    DestroyRunResponse,
    ExecuteCommandRequest,
    RunResultsResponse,
    RunStatus,
    UploadWorkspaceResponse,
)

MAX_EXTRACTED_FILES = 25_000
_SECRET_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,255}|github_pat_[A-Za-z0-9_]{16,255})\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9\-_]{16,255}|lsv2_[A-Za-z0-9\-_]{16,255})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class SandboxNotFoundError(Exception):
    """Raised when a requested sandbox run does not exist."""


class SandboxValidationError(Exception):
    """Raised when an archive, command argument, or run transition is invalid."""


@dataclass
class _SandboxRunState:
    run_id: str
    workflow_id: str
    repository_id: str
    base_commit_sha: str
    image: str
    cpu_limit: float
    memory_limit_mb: int
    pids_limit: int
    timeout_seconds: int
    run_dir: Path
    workspace_dir: Path
    venv_dir: Path
    created_at: datetime
    status: RunStatus = "created"
    workspace_uploaded: bool = False
    executions: list[CommandExecutionRecord] = field(default_factory=list)


def _redact_output(text: str, max_chars: int) -> str:
    scrubbed = text
    for pattern in _SECRET_LINE_PATTERNS:
        scrubbed = pattern.sub("[REDACTED]", scrubbed)
    if len(scrubbed) > max_chars:
        return scrubbed[:max_chars] + "\n[sandbox-runner] Output truncated due to size limit."
    return scrubbed


def _safe_rmtree(directory: Path) -> None:
    if not directory.exists():
        return
    for root, dirs, files in os.walk(directory):
        for dir_name in dirs:
            try:
                os.chmod(Path(root) / dir_name, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                continue
        for file_name in files:
            file_path = Path(root) / file_name
            if not file_path.is_symlink():
                try:
                    os.chmod(file_path, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    continue
    shutil.rmtree(directory)


def _validate_archive_member_path(raw_name: str) -> str:
    cleaned = raw_name.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or "\x00" in cleaned or cleaned.startswith("/"):
        raise SandboxValidationError(f"Forbidden archive member path: '{raw_name}'.")
    if ":" in cleaned.split("/", 1)[0]:
        raise SandboxValidationError(f"Drive-qualified archive member path: '{raw_name}'.")

    parts = [p for p in cleaned.split("/") if p not in {"", "."}]
    if not parts or any(p == ".." for p in parts):
        raise SandboxValidationError(f"Path traversal detected in archive: '{raw_name}'.")
    if parts[0] == ".git":
        raise SandboxValidationError("Archive must not contain '.git' directory internals.")

    leaf_lower = parts[-1].lower()
    if leaf_lower == ".env" or leaf_lower.startswith(".env."):
        raise SandboxValidationError(
            f"Sensitive environment file '{raw_name}' is not permitted in sandbox workspace."
        )
    return "/".join(parts)


class SandboxRunManager:
    """Coordinates isolated sandbox runs, safe archive extraction, and cleanup."""

    def __init__(
        self,
        *,
        settings: SandboxRunnerSettings,
        runtime: ContainerRuntime,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._runs_root = settings.SANDBOX_RUNS_ROOT.resolve()
        self._runs_root.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, _SandboxRunState] = {}

    def _get_run(self, run_id: str) -> _SandboxRunState:
        state = self._runs.get(run_id)
        if state is None:
            raise SandboxNotFoundError(f"Sandbox run '{run_id}' not found.")
        return state

    def create_run(self, payload: CreateRunRequest) -> CreateRunResponse:
        """Allocate isolated workspace and virtualenv directories for a new run."""
        run_id = uuid.uuid4().hex
        try:
            image = validate_image_name(payload.image or self._settings.SANDBOX_IMAGE)
        except ValueError as exc:
            raise SandboxValidationError(str(exc)) from exc

        run_dir = (self._runs_root / run_id).resolve()
        if not run_dir.is_relative_to(self._runs_root):
            raise SandboxValidationError("Resolved run directory escaped configured root.")

        workspace_dir = run_dir / "workspace"
        venv_dir = run_dir / "venv"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        venv_dir.mkdir(parents=True, exist_ok=True)

        # Allow non-root container UID (65534) to write installed packages into the run venv dir
        with contextlib.suppress(OSError):
            os.chmod(venv_dir, 0o777)  # noqa: S103  # nosec B103

        now = datetime.now(UTC)
        state = _SandboxRunState(
            run_id=run_id,
            workflow_id=payload.workflow_id,
            repository_id=payload.repository_id,
            base_commit_sha=payload.base_commit_sha,
            image=image,
            cpu_limit=min(
                payload.cpu_limit or self._settings.SANDBOX_CPU_LIMIT,
                self._settings.SANDBOX_CPU_LIMIT,
            ),
            memory_limit_mb=min(
                payload.memory_limit_mb or self._settings.SANDBOX_MEMORY_LIMIT_MB,
                self._settings.SANDBOX_MEMORY_LIMIT_MB,
            ),
            pids_limit=min(
                payload.pids_limit or self._settings.SANDBOX_PIDS_LIMIT,
                self._settings.SANDBOX_PIDS_LIMIT,
            ),
            timeout_seconds=min(
                payload.timeout_seconds or self._settings.SANDBOX_TEST_TIMEOUT_SECONDS,
                self._settings.SANDBOX_TEST_TIMEOUT_SECONDS,
            ),
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            venv_dir=venv_dir,
            created_at=now,
        )
        self._runs[run_id] = state
        return CreateRunResponse(
            run_id=run_id,
            status=state.status,
            image=state.image,
            created_at=now,
        )

    def upload_workspace_archive(
        self,
        run_id: str,
        archive_bytes: bytes,
    ) -> UploadWorkspaceResponse:
        """Safely validate and extract a gzip tar archive without symlinks or traversal."""
        state = self._get_run(run_id)
        if len(archive_bytes) == 0:
            raise SandboxValidationError("Workspace archive payload is empty.")
        if len(archive_bytes) > self._settings.SANDBOX_MAX_ARCHIVE_BYTES:
            raise SandboxValidationError(
                "Workspace archive exceeds maximum allowed upload size."
            )

        max_disk_bytes = self._settings.SANDBOX_DISK_LIMIT_MB * 1024 * 1024
        files_extracted = 0
        total_bytes = 0

        _safe_rmtree(state.workspace_dir)
        state.workspace_dir.mkdir(parents=True, exist_ok=True)
        workspace_root = state.workspace_dir.resolve()

        try:
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if member.isdir():
                        if member.name.strip() in {"", ".", "./"}:
                            continue
                        rel_dir = _validate_archive_member_path(member.name)
                        dest_dir = (workspace_root / rel_dir).resolve()
                        if not dest_dir.is_relative_to(workspace_root):
                            raise SandboxValidationError("Archive directory escapes workspace.")
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        continue

                    if not member.isfile():
                        raise SandboxValidationError(
                            f"Unsupported archive entry (symlink/device): '{member.name}'."
                        )

                    rel_path = _validate_archive_member_path(member.name)
                    files_extracted += 1
                    if files_extracted > MAX_EXTRACTED_FILES:
                        raise SandboxValidationError(
                            "Archive exceeds maximum allowed file count."
                        )

                    dest_file = (workspace_root / rel_path).resolve()
                    if not dest_file.is_relative_to(workspace_root):
                        raise SandboxValidationError("Archive file escapes workspace boundary.")

                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    extracted_stream = tar.extractfile(member)
                    if extracted_stream is None:
                        continue

                    with extracted_stream, dest_file.open("wb") as out_fh:
                        while True:
                            chunk = extracted_stream.read(65_536)
                            if not chunk:
                                break
                            total_bytes += len(chunk)
                            if total_bytes > max_disk_bytes:
                                raise SandboxValidationError(
                                    "Extracted workspace exceeds sandbox disk quota."
                                )
                            out_fh.write(chunk)
        except (tarfile.TarError, EOFError, OSError) as exc:
            _safe_rmtree(state.workspace_dir)
            state.workspace_dir.mkdir(parents=True, exist_ok=True)
            raise SandboxValidationError(
                f"Invalid or corrupted gzip tar archive: {type(exc).__name__}"
            ) from exc
        except SandboxValidationError:
            _safe_rmtree(state.workspace_dir)
            state.workspace_dir.mkdir(parents=True, exist_ok=True)
            raise

        state.workspace_uploaded = True
        state.status = "ready"
        return UploadWorkspaceResponse(
            run_id=run_id,
            status=state.status,
            files_extracted=files_extracted,
            total_bytes=total_bytes,
        )

    def execute_command(
        self,
        run_id: str,
        payload: ExecuteCommandRequest,
    ) -> CommandExecutionRecord:
        """Execute an allowlisted command template inside a hardened container."""
        state = self._get_run(run_id)
        if not state.workspace_uploaded:
            raise SandboxValidationError(
                "Workspace archive must be uploaded before executing commands."
            )

        try:
            phase, network_policy, command_argv = build_template_command(
                template=payload.template,
                target_paths=payload.target_paths,
                pytest_args=payload.pytest_args,
                extra_pip_packages=payload.extra_pip_packages,
                pip_index_url=self._settings.SANDBOX_PIP_INDEX_URL,
            )
        except ValueError as exc:
            raise SandboxValidationError(str(exc)) from exc

        execution_id = uuid.uuid4().hex
        effective_timeout = min(
            payload.timeout_seconds or state.timeout_seconds,
            state.timeout_seconds,
        )
        spec = ContainerExecutionSpec(
            run_id=state.run_id,
            execution_id=execution_id,
            template=payload.template,
            phase=phase,
            network_policy=network_policy,
            image=state.image,
            workspace_dir=state.workspace_dir,
            venv_dir=state.venv_dir,
            command_argv=command_argv,
            cpu_limit=state.cpu_limit,
            memory_limit_mb=state.memory_limit_mb,
            pids_limit=state.pids_limit,
            timeout_seconds=effective_timeout,
            install_network=self._settings.SANDBOX_INSTALL_NETWORK,
            seccomp_profile=self._settings.SANDBOX_SECCOMP_PROFILE,
        )

        state.status = "running"
        started_at = datetime.now(UTC)
        outcome = self._runtime.execute(spec)
        finished_at = datetime.now(UTC)

        record = CommandExecutionRecord(
            execution_id=execution_id,
            template=payload.template,
            phase=phase,
            network_policy=network_policy,
            command_argv=command_argv,
            exit_code=outcome.exit_code,
            stdout=_redact_output(outcome.stdout, self._settings.SANDBOX_MAX_OUTPUT_CHARS),
            stderr=_redact_output(outcome.stderr, self._settings.SANDBOX_MAX_OUTPUT_CHARS),
            duration_seconds=outcome.duration_seconds,
            timed_out=outcome.timed_out,
            started_at=started_at,
            finished_at=finished_at,
        )
        state.executions.append(record)
        state.status = "completed" if outcome.exit_code == 0 else "failed"
        return record

    def get_results(self, run_id: str) -> RunResultsResponse:
        """Fetch current status and all captured command execution records for a run."""
        state = self._get_run(run_id)
        return RunResultsResponse(
            run_id=state.run_id,
            workflow_id=state.workflow_id,
            repository_id=state.repository_id,
            base_commit_sha=state.base_commit_sha,
            status=state.status,
            workspace_uploaded=state.workspace_uploaded,
            executions=list(state.executions),
        )

    def destroy_run(self, run_id: str) -> DestroyRunResponse:
        """Delete all temporary workspace/venv files and remove run state."""
        state = self._runs.pop(run_id, None)
        if state is None:
            return DestroyRunResponse(run_id=run_id, destroyed=False)
        _safe_rmtree(state.run_dir)
        state.status = "destroyed"
        return DestroyRunResponse(run_id=run_id, destroyed=True)