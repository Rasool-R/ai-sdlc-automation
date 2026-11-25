"""Validated runtime configuration for the isolated sandbox-runner service."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SandboxRunnerSettings(BaseSettings):
    """Configuration controlling container limits, storage bounds, and network policy."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    SANDBOX_RUNS_ROOT: Path = Path("./.sandbox_runs")
    SANDBOX_IMAGE: str = "python:3.11-slim"
    SANDBOX_CPU_LIMIT: float = Field(default=2.0, gt=0.1, le=16.0)
    SANDBOX_MEMORY_LIMIT_MB: int = Field(default=2048, ge=256, le=32768)
    SANDBOX_DISK_LIMIT_MB: int = Field(default=1024, ge=64, le=16384)
    SANDBOX_PIDS_LIMIT: int = Field(default=256, ge=32, le=4096)
    SANDBOX_TEST_TIMEOUT_SECONDS: int = Field(default=600, ge=10, le=3600)
    SANDBOX_INSTALL_NETWORK: str = "bridge"
    SANDBOX_PIP_INDEX_URL: str = "https://pypi.org/simple"
    SANDBOX_MAX_ARCHIVE_BYTES: int = Field(default=50 * 1024 * 1024, ge=1024)
    SANDBOX_MAX_OUTPUT_CHARS: int = Field(default=200_000, ge=1024)
    SANDBOX_INTERNAL_TOKEN: str = ""
    SANDBOX_DOCKER_BIN: str = "docker"
    SANDBOX_SECCOMP_PROFILE: str = ""


@lru_cache(maxsize=1)
def get_sandbox_settings() -> SandboxRunnerSettings:
    """Return cached singleton settings for the sandbox-runner service."""
    return SandboxRunnerSettings()