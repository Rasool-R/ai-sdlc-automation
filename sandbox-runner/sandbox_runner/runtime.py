"""Hardened Docker container execution runtime enforcing two-phase network isolation."""

import re
import shutil
import subprocess  # noqa: S404  # nosec B404
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sandbox_runner.config import SandboxRunnerSettings
from sandbox_runner.schemas import CommandTemplate, ExecutionPhase, NetworkPolicy

_ALLOWED_PYTEST_ARGS: frozenset[str] = frozenset(
    {
        "-q",
        "-v",
        "-vv",
        "-x",
        "-s",
        "--tb=short",
        "--tb=line",
        "--tb=no",
        "--maxfail=1",
        "--maxfail=3",
        "--maxfail=5",
        "--no-header",
    }
)
_SAFE_TARGET_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]{1,256}$")
_SAFE_PACKAGE_SPEC_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:==[A-Za-z0-9._-]+)?$"
)
_SAFE_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{1,254}$")
_SAFE_SECCOMP_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_./:-]{1,256}$")

_CONTAINER_TMPFS_SPEC = "/tmp:rw,noexec,nosuid,size=256m"  # noqa: S108  # nosec B108
_CONTAINER_TMP_HOME = "/tmp"  # noqa: S108  # nosec B108
_CONTAINER_PYTEST_CACHE = "/tmp/pytest_cache"  # noqa: S108  # nosec B108
_CONTAINER_RUFF_CACHE = "/tmp/ruff_cache"  # noqa: S108  # nosec B108
_CONTAINER_MYPY_CACHE = "/tmp/mypy_cache"  # noqa: S108  # nosec B108

# Platform-enforced Ruff rule categories; --isolated prevents repository config overrides.
_PLATFORM_RUFF_RULES = "E,F,B,S,UP"

# Platform-controlled dependency bootstrap script executed inside the install container.
# Untrusted user strings are never interpolated into source code; validated args arrive via argv.
_INSTALL_BOOTSTRAP_SCRIPT = """
import pathlib, subprocess, sys

index_url = sys.argv[1]
extra_pkgs = sys.argv[2:]
venv_dir = pathlib.Path("/sandbox_venv")
venv_python = venv_dir / "bin" / "python"

if not venv_python.exists():
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

pip_base = [
    str(venv_python), "-m", "pip", "install",
    "--disable-pip-version-check",
    "--index-url", index_url,
]
ws = pathlib.Path("/workspace")
pyproject = ws / "pyproject.toml"
reqs = ws / "requirements.txt"
dev_reqs = ws / "requirements-dev.txt"

if pyproject.is_file():
    res = subprocess.run([*pip_base, "--no-build-isolation", "-e", "/workspace[dev]"])
    if res.returncode != 0:
        subprocess.run([*pip_base, "/workspace[dev]"], check=False)
        subprocess.run([*pip_base, "/workspace"], check=True)
elif reqs.is_file():
    subprocess.run([*pip_base, "-r", str(reqs)], check=True)
    if dev_reqs.is_file():
        subprocess.run([*pip_base, "-r", str(dev_reqs)], check=True)

if extra_pkgs:
    subprocess.run([*pip_base, *extra_pkgs], check=True)
""".strip()


@dataclass(frozen=True)
class ContainerExecutionSpec:
    """Validated specification describing a single hardened container execution."""

    run_id: str
    execution_id: str
    template: CommandTemplate
    phase: ExecutionPhase
    network_policy: NetworkPolicy
    image: str
    workspace_dir: Path
    venv_dir: Path
    command_argv: list[str]
    cpu_limit: float
    memory_limit_mb: int
    pids_limit: int
    timeout_seconds: int
    install_network: str
    seccomp_profile: str = ""


@dataclass(frozen=True)
class RawExecutionOutcome:
    """Raw process outcome returned by the container runtime adapter."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class ContainerRuntime(Protocol):
    """Protocol abstracting container execution for testability and runtime isolation."""

    def execute(self, spec: ContainerExecutionSpec) -> RawExecutionOutcome:
        """Execute a hardened container for the given spec and clean it up on exit."""


def validate_image_name(image: str) -> str:
    """Validate container image reference format."""
    cleaned = image.strip()
    if not _SAFE_IMAGE_PATTERN.match(cleaned) or ".." in cleaned:
        raise ValueError(f"Invalid container image reference: '{image}'.")
    return cleaned


def validate_target_paths(paths: list[str]) -> list[str]:
    """Validate relative POSIX target paths preventing traversal or option injection."""
    validated: list[str] = []
    for raw in paths:
        cleaned = raw.replace("\\", "/").strip()
        if (
            not cleaned
            or cleaned.startswith(("/", "-"))
            or ".." in cleaned.split("/")
            or not _SAFE_TARGET_PATH_PATTERN.match(cleaned)
        ):
            raise ValueError(f"Unsafe or invalid target path: '{raw}'.")
        validated.append(cleaned)
    return validated


def validate_pytest_args(args: list[str]) -> list[str]:
    """Ensure every pytest CLI argument belongs to the strict platform allowlist."""
    validated: list[str] = []
    for arg in args:
        cleaned = arg.strip()
        if cleaned not in _ALLOWED_PYTEST_ARGS:
            raise ValueError(
                f"Pytest argument '{arg}' is not permitted by sandbox allowlist."
            )
        validated.append(cleaned)
    return validated


def validate_extra_pip_packages(packages: list[str]) -> list[str]:
    """Ensure optional extra pip packages are plain pinned/named packages without URLs/flags."""
    validated: list[str] = []
    for pkg in packages:
        cleaned = pkg.strip()
        if cleaned.startswith("-") or not _SAFE_PACKAGE_SPEC_PATTERN.match(cleaned):
            raise ValueError(
                f"Invalid or unsafe pip package specifier: '{pkg}'."
            )
        validated.append(cleaned)
    return validated


def build_template_command(
    *,
    template: CommandTemplate,
    target_paths: list[str],
    pytest_args: list[str],
    extra_pip_packages: list[str],
    pip_index_url: str,
) -> tuple[ExecutionPhase, NetworkPolicy, list[str]]:
    """Build deterministic argv and enforce two-phase network policy per command template."""
    safe_paths = validate_target_paths(target_paths)
    safe_pytest_args = validate_pytest_args(pytest_args)
    safe_pkgs = validate_extra_pip_packages(extra_pip_packages)

    if template == "install_deps":
        return (
            "dependency_install",
            "restricted_egress",
            ["python", "-c", _INSTALL_BOOTSTRAP_SCRIPT, pip_index_url, *safe_pkgs],
        )

    if template == "pytest":
        argv = [
            "/sandbox_venv/bin/python",
            "-m",
            "pytest",
            "-o",
            f"cache_dir={_CONTAINER_PYTEST_CACHE}",
            *safe_pytest_args,
            *safe_paths,
        ]
        return ("isolated_execution", "none", argv)

    if template == "ruff_check":
        paths = safe_paths or ["."]
        # --isolated prevents untrusted repo configs from weakening security/lint rules.
        argv = [
            "/sandbox_venv/bin/python",
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--no-fix",
            "--select",
            _PLATFORM_RUFF_RULES,
            "--output-format",
            "json",
            "--cache-dir",
            _CONTAINER_RUFF_CACHE,
            *paths,
        ]
        return ("isolated_execution", "none", argv)

    if template == "mypy_check":
        paths = safe_paths or ["."]
        argv = [
            "/sandbox_venv/bin/python",
            "-m",
            "mypy",
            "--cache-dir",
            _CONTAINER_MYPY_CACHE,
            "--no-color-output",
            "--no-error-summary",
            "--show-column-numbers",
            "--show-error-codes",
            "--ignore-missing-imports",
            *paths,
        ]
        return ("isolated_execution", "none", argv)

    if template == "bandit_check":
        paths = safe_paths or ["."]
        argv = [
            "/sandbox_venv/bin/python",
            "-m",
            "bandit",
            "-r",
            "-f",
            "json",
            "-q",
            *paths,
        ]
        return ("isolated_execution", "none", argv)

    raise ValueError(f"Unsupported command template: '{template}'.")


def build_docker_run_argv(
    spec: ContainerExecutionSpec,
    *,
    docker_bin: str = "docker",
) -> tuple[str, list[str]]:
    """Assemble hardened `docker run` CLI arguments without shell interpolation."""
    container_name = f"sdlc-sbx-{spec.run_id[:12]}-{spec.execution_id[:8]}"
    network_arg = "none" if spec.network_policy == "none" else spec.install_network

    security_opts = ["--security-opt=no-new-privileges:true"]
    seccomp = spec.seccomp_profile.strip()
    if seccomp:
        if ".." in seccomp or not _SAFE_SECCOMP_PATH_PATTERN.match(seccomp):
            raise ValueError("Invalid seccomp profile path configured.")
        security_opts.append(f"--security-opt=seccomp={seccomp}")

    argv = [
        docker_bin,
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        network_arg,
        "--cpus",
        str(spec.cpu_limit),
        "--memory",
        f"{spec.memory_limit_mb}m",
        "--memory-swap",
        f"{spec.memory_limit_mb}m",
        "--pids-limit",
        str(spec.pids_limit),
        "--cap-drop=ALL",
        *security_opts,
        "--read-only",
        "--user",
        "65534:65534",
        "--tmpfs",
        _CONTAINER_TMPFS_SPEC,
        "-v",
        f"{spec.workspace_dir.resolve()}:/workspace:ro",
        "-v",
        f"{spec.venv_dir.resolve()}:/sandbox_venv:rw",
        "-w",
        "/workspace",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        f"HOME={_CONTAINER_TMP_HOME}",
        "-e",
        "VIRTUAL_ENV=/sandbox_venv",
        spec.image,
        *spec.command_argv,
    ]
    return container_name, argv


class DockerCliRuntime:
    """Executes hardened ephemeral containers through the configured Docker CLI binary."""

    def __init__(self, settings: SandboxRunnerSettings) -> None:
        self._settings = settings

    def _resolve_docker_bin(self) -> str:
        resolved = shutil.which(self._settings.SANDBOX_DOCKER_BIN)
        if not resolved:
            raise RuntimeError(
                f"Docker CLI binary '{self._settings.SANDBOX_DOCKER_BIN}' not found in PATH."
            )
        return resolved

    def _force_remove_container(self, docker_bin: str, container_name: str) -> None:
        try:
            subprocess.run(  # noqa: S603  # nosec B603
                [docker_bin, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return

    def execute(self, spec: ContainerExecutionSpec) -> RawExecutionOutcome:
        """Run a hardened container with strict timeout enforcement and guaranteed cleanup."""
        docker_bin = self._resolve_docker_bin()
        container_name, argv = build_docker_run_argv(spec, docker_bin=docker_bin)

        start_monotonic = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(  # noqa: S603  # nosec B603
                argv,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
                check=False,
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr or "")
            )
            stderr = f"{stderr}\n[sandbox-runner] Execution timed out after {spec.timeout_seconds}s"
        finally:
            self._force_remove_container(docker_bin, container_name)

        duration = round(max(0.0, time.monotonic() - start_monotonic), 4)
        return RawExecutionOutcome(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
        )