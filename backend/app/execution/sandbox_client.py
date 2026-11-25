"""Backend HTTP client for the isolated sandbox-runner service."""

import io
import logging
import os
import tarfile
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, ValidationFailedError

logger = logging.getLogger(__name__)

SandboxTemplate = Literal[
    "install_deps",
    "pytest",
    "ruff_check",
    "mypy_check",
    "bandit_check",
]

DEFAULT_ANALYSIS_TOOLS: tuple[str, ...] = ("ruff", "mypy", "bandit")

_EXCLUDED_ARCHIVE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
)
_EXCLUDED_SECRET_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".p12", ".pfx"})


class SandboxExecutionResult(BaseModel):
    """Result returned by sandbox-runner for a single executed command template."""

    execution_id: str
    template: SandboxTemplate
    phase: Literal["dependency_install", "isolated_execution"]
    network_policy: Literal["restricted_egress", "none"]
    command_argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0.0)
    timed_out: bool = False


class SandboxRunReport(BaseModel):
    """Aggregated report for an isolated test run executed inside the sandbox."""

    run_id: str
    workflow_id: str
    repository_id: str
    base_commit_sha: str
    passed: bool
    install_result: SandboxExecutionResult | None = None
    test_result: SandboxExecutionResult | None = None
    executions: list[SandboxExecutionResult] = Field(default_factory=list)


class SandboxStaticAnalysisRawReport(BaseModel):
    """Raw sandbox execution outputs for deterministic offline static-analysis tools."""

    run_id: str
    workflow_id: str
    repository_id: str
    base_commit_sha: str
    install_result: SandboxExecutionResult | None = None
    tool_results: dict[str, SandboxExecutionResult] = Field(default_factory=dict)


def package_workspace_archive(
    workspace_dir: Path,
    *,
    max_archive_bytes: int = 50 * 1024 * 1024,
) -> bytes:
    """Package a workspace directory into a gzip tar archive excluding .git and secret files."""
    root = workspace_dir.resolve()
    if not root.is_dir():
        raise ValidationFailedError(
            "Workspace directory does not exist for sandbox packaging.",
            details={"workspace_dir": str(workspace_dir)},
        )

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for current_root, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_ARCHIVE_DIRS)
            for filename in sorted(files):
                file_path = Path(current_root) / filename
                if file_path.is_symlink():
                    continue
                resolved = file_path.resolve()
                if not resolved.is_relative_to(root):
                    continue

                name_lower = filename.lower()
                if (
                    name_lower == ".env"
                    or name_lower.startswith(".env.")
                    or file_path.suffix.lower() in _EXCLUDED_SECRET_SUFFIXES
                ):
                    continue

                rel_posix = file_path.relative_to(root).as_posix()
                tar.add(str(file_path), arcname=rel_posix, recursive=False)

    archive_bytes = buffer.getvalue()
    if len(archive_bytes) > max_archive_bytes:
        raise ValidationFailedError(
            "Packaged workspace archive exceeds maximum sandbox upload size.",
            details={
                "archive_bytes": len(archive_bytes),
                "max_archive_bytes": max_archive_bytes,
            },
        )
    return archive_bytes


class SandboxClient:
    """Thin client delegating all untrusted code execution to the sandbox-runner service."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
        internal_token: str = "",
        timeout_seconds: float | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.SANDBOX_RUNNER_URL.rstrip("/")
        self._internal_token = internal_token.strip()
        self._owns_client = http_client is None
        effective_timeout = timeout_seconds or float(
            self._settings.SANDBOX_TEST_TIMEOUT_SECONDS + 30
        )
        self._http = http_client or httpx.Client(
            base_url=self._base_url,
            timeout=effective_timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        """Close the HTTP client if owned by this instance."""
        if self._owns_client:
            self._http.close()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._internal_token:
            headers["X-Sandbox-Token"] = self._internal_token
        if extra:
            headers.update(extra)
        return headers

    def _parse_response(self, response: httpx.Response, *, action: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise AppError(
                f"Sandbox runner rejected '{action}' with HTTP {response.status_code}.",
                code="SANDBOX_RUNNER_ERROR",
                status_code=502,
                details={"action": action, "upstream_status": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppError(
                f"Sandbox runner returned invalid JSON for '{action}'.",
                code="SANDBOX_INVALID_RESPONSE",
                status_code=502,
                details={"action": action},
            ) from exc
        if not isinstance(payload, dict):
            raise AppError(
                f"Unexpected response format from sandbox runner for '{action}'.",
                code="SANDBOX_INVALID_RESPONSE",
                status_code=502,
                details={"action": action},
            )
        return payload

    def create_run(
        self,
        *,
        workflow_id: str,
        repository_id: str,
        base_commit_sha: str,
        timeout_seconds: int | None = None,
    ) -> str:
        """Allocate a new isolated sandbox run and return its run_id."""
        body = {
            "workflow_id": workflow_id,
            "repository_id": repository_id,
            "base_commit_sha": base_commit_sha,
            "image": self._settings.SANDBOX_IMAGE,
            "cpu_limit": self._settings.SANDBOX_CPU_LIMIT,
            "memory_limit_mb": self._settings.SANDBOX_MEMORY_LIMIT_MB,
            "pids_limit": self._settings.SANDBOX_PIDS_LIMIT,
            "timeout_seconds": timeout_seconds or self._settings.SANDBOX_TEST_TIMEOUT_SECONDS,
        }
        try:
            response = self._http.post(
                f"{self._base_url}/runs",
                json=body,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise AppError(
                "Failed to connect to sandbox runner service.",
                code="SANDBOX_CONNECTION_ERROR",
                status_code=502,
                details={"action": "create_run", "reason": type(exc).__name__},
            ) from exc

        data = self._parse_response(response, action="create_run")
        return str(data["run_id"])

    def upload_workspace(self, run_id: str, workspace_dir: Path) -> dict[str, Any]:
        """Package and upload a workspace archive to an allocated sandbox run."""
        archive_bytes = package_workspace_archive(workspace_dir)
        try:
            response = self._http.post(
                f"{self._base_url}/runs/{run_id}/workspace",
                content=archive_bytes,
                headers=self._headers({"Content-Type": "application/gzip"}),
            )
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error uploading workspace archive to sandbox runner.",
                code="SANDBOX_CONNECTION_ERROR",
                status_code=502,
                details={"action": "upload_workspace", "reason": type(exc).__name__},
            ) from exc
        return self._parse_response(response, action="upload_workspace")

    def execute_command(
        self,
        run_id: str,
        *,
        template: SandboxTemplate,
        target_paths: list[str] | None = None,
        pytest_args: list[str] | None = None,
        extra_pip_packages: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> SandboxExecutionResult:
        """Invoke an allowlisted command template inside the sandbox run."""
        body: dict[str, Any] = {
            "template": template,
            "target_paths": target_paths or [],
            "pytest_args": pytest_args or [],
            "extra_pip_packages": extra_pip_packages or [],
        }
        if timeout_seconds is not None:
            body["timeout_seconds"] = timeout_seconds

        try:
            response = self._http.post(
                f"{self._base_url}/runs/{run_id}/execute",
                json=body,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error executing command in sandbox runner.",
                code="SANDBOX_CONNECTION_ERROR",
                status_code=502,
                details={"action": f"execute:{template}", "reason": type(exc).__name__},
            ) from exc

        data = self._parse_response(response, action=f"execute:{template}")
        return SandboxExecutionResult.model_validate(data)

    def destroy_run(self, run_id: str) -> bool:
        """Destroy an allocated sandbox run and remove all temporary files."""
        try:
            response = self._http.delete(
                f"{self._base_url}/runs/{run_id}",
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error destroying sandbox run.",
                code="SANDBOX_CONNECTION_ERROR",
                status_code=502,
                details={"action": "destroy_run", "reason": type(exc).__name__},
            ) from exc

        data = self._parse_response(response, action="destroy_run")
        return bool(data.get("destroyed", False))

    def run_isolated_tests(
        self,
        *,
        workflow_id: str,
        repository_id: str,
        base_commit_sha: str,
        workspace_dir: Path,
        install_dependencies: bool = True,
        pytest_target_paths: list[str] | None = None,
        pytest_args: list[str] | None = None,
    ) -> SandboxRunReport:
        """Execute two-phase dependency install and offline pytest with guaranteed cleanup."""
        run_id = self.create_run(
            workflow_id=workflow_id,
            repository_id=repository_id,
            base_commit_sha=base_commit_sha,
        )
        executions: list[SandboxExecutionResult] = []
        install_result: SandboxExecutionResult | None = None
        test_result: SandboxExecutionResult | None = None

        try:
            self.upload_workspace(run_id, workspace_dir)

            if install_dependencies:
                install_result = self.execute_command(run_id, template="install_deps")
                executions.append(install_result)
                if install_result.exit_code != 0:
                    return SandboxRunReport(
                        run_id=run_id,
                        workflow_id=workflow_id,
                        repository_id=repository_id,
                        base_commit_sha=base_commit_sha,
                        passed=False,
                        install_result=install_result,
                        test_result=None,
                        executions=executions,
                    )

            test_result = self.execute_command(
                run_id,
                template="pytest",
                target_paths=pytest_target_paths or ["tests"],
                pytest_args=pytest_args or ["-q"],
            )
            executions.append(test_result)

            return SandboxRunReport(
                run_id=run_id,
                workflow_id=workflow_id,
                repository_id=repository_id,
                base_commit_sha=base_commit_sha,
                passed=test_result.exit_code == 0,
                install_result=install_result,
                test_result=test_result,
                executions=executions,
            )
        finally:
            try:
                self.destroy_run(run_id)
            except AppError as exc:
                logger.warning(
                    "Failed to clean up sandbox run after completion",
                    extra={"structured_data": {"run_id": run_id, "code": exc.code}},
                )

    def run_static_analysis(
        self,
        *,
        workflow_id: str,
        repository_id: str,
        base_commit_sha: str,
        workspace_dir: Path,
        target_paths: list[str] | None = None,
        install_dependencies: bool = True,
        extra_analysis_packages: list[str] | None = None,
    ) -> SandboxStaticAnalysisRawReport:
        """Install project + analyzer deps, then run offline Ruff, Mypy, and Bandit."""
        run_id = self.create_run(
            workflow_id=workflow_id,
            repository_id=repository_id,
            base_commit_sha=base_commit_sha,
        )
        install_result: SandboxExecutionResult | None = None
        tool_results: dict[str, SandboxExecutionResult] = {}
        effective_targets = target_paths or ["."]
        analyzer_pkgs = (
            list(extra_analysis_packages)
            if extra_analysis_packages is not None
            else list(DEFAULT_ANALYSIS_TOOLS)
        )

        try:
            self.upload_workspace(run_id, workspace_dir)

            if install_dependencies:
                install_result = self.execute_command(
                    run_id,
                    template="install_deps",
                    extra_pip_packages=analyzer_pkgs,
                )
                if install_result.exit_code != 0:
                    return SandboxStaticAnalysisRawReport(
                        run_id=run_id,
                        workflow_id=workflow_id,
                        repository_id=repository_id,
                        base_commit_sha=base_commit_sha,
                        install_result=install_result,
                        tool_results={},
                    )

            for tool_name, template in (
                ("ruff", "ruff_check"),
                ("mypy", "mypy_check"),
                ("bandit", "bandit_check"),
            ):
                tool_results[tool_name] = self.execute_command(
                    run_id,
                    template=template,  # type: ignore[arg-type]
                    target_paths=effective_targets,
                )

            return SandboxStaticAnalysisRawReport(
                run_id=run_id,
                workflow_id=workflow_id,
                repository_id=repository_id,
                base_commit_sha=base_commit_sha,
                install_result=install_result,
                tool_results=tool_results,
            )
        finally:
            try:
                self.destroy_run(run_id)
            except AppError as exc:
                logger.warning(
                    "Failed to clean up static-analysis sandbox run",
                    extra={"structured_data": {"run_id": run_id, "code": exc.code}},
                )