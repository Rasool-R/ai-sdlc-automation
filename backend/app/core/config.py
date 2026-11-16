"""Validated application configuration with secret-safe error reporting."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import ConfigurationError


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Core Application & Security
    APP_ENV: Literal["development", "test", "production"] = "development"
    APP_NAME: str = "ai-sdlc-automation"
    APP_SECRET_KEY: str = Field(
        default="dev-only-secret-key-at-least-32-characters-long",
        min_length=32,
    )
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"

    # Single-Operator Authentication
    OPERATOR_USERNAME: str = Field(default="admin", min_length=3)
    OPERATOR_PASSWORD_HASH: str = Field(
        default="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",
        min_length=20,
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60, ge=5, le=1440)

    # Credential Encryption (Comma-separated Fernet keys; first is primary)
    CREDENTIAL_ENCRYPTION_KEYS: str = Field(
        default="7_wB9x_0yZ1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p8=",
        min_length=44,
    )

    # Infrastructure
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/ai_sdlc",
        min_length=10,
    )
    REDIS_URL: str = Field(default="redis://localhost:6379/0", min_length=8)
    CELERY_BROKER_URL: str = Field(default="redis://localhost:6379/1", min_length=8)
    CELERY_RESULT_BACKEND: str = Field(default="redis://localhost:6379/2", min_length=8)
    QDRANT_URL: str = Field(default="http://localhost:6333", min_length=8)
    QDRANT_API_KEY: str = ""

    # LLM, Embeddings, and Tracing
    LLM_PROVIDER: Literal["openai", "anthropic", "google", "local"] = "openai"
    LLM_MODEL_DEFAULT: str = "gpt-4o"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""

    EMBEDDING_PROVIDER: Literal["openai", "local", "disabled"] = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_API_KEY: str = ""

    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "ai-sdlc-automation"

    # GitHub Integration
    GITHUB_AUTH_MODE: Literal["app", "pat_fallback", "disabled"] = "disabled"
    GITHUB_APP_ID: str = ""
    GITHUB_APP_INSTALLATION_ID: str = ""
    GITHUB_APP_PRIVATE_KEY_PATH: str = ""
    GITHUB_PAT_FALLBACK_TOKEN: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""

    # Workspaces, Artifacts, and Sandbox Runner
    WORKSPACE_ROOT_DIR: Path = Path("./.workspaces")
    WORKSPACE_DISK_QUOTA_MB: int = Field(default=2048, ge=128)
    ARTIFACT_STORAGE_BACKEND: Literal["local"] = "local"
    ARTIFACT_STORAGE_PATH: Path = Path("./.artifacts")

    SANDBOX_RUNNER_URL: str = "http://localhost:8001"
    SANDBOX_IMAGE: str = "python:3.12-slim"
    SANDBOX_CPU_LIMIT: float = Field(default=2.0, gt=0.1, le=16.0)
    SANDBOX_MEMORY_LIMIT_MB: int = Field(default=2048, ge=256, le=32768)
    SANDBOX_DISK_LIMIT_MB: int = Field(default=1024, ge=128, le=16384)
    SANDBOX_PIDS_LIMIT: int = Field(default=256, ge=32, le=4096)
    SANDBOX_TEST_TIMEOUT_SECONDS: int = Field(default=600, ge=10, le=3600)
    SANDBOX_PIP_INDEX_URL: str = "https://pypi.org/simple"

    # Default Operational Budgets and Safety Limits
    MAX_CORRECTION_LOOPS_PER_FAILURE: int = Field(default=3, ge=1, le=10)
    MAX_CORRECTION_LOOPS_PER_WORKFLOW: int = Field(default=5, ge=1, le=20)
    MAX_LLM_CALLS_PER_WORKFLOW: int = Field(default=150, ge=1, le=1000)
    MAX_FILES_PER_PATCH: int = Field(default=15, ge=1, le=100)
    MAX_LINES_PER_PATCH: int = Field(default=1500, ge=10, le=20000)
    MAX_RETRIEVAL_TOKENS: int = Field(default=24000, ge=1000, le=200000)
    MAX_RETRIEVAL_FILES: int = Field(default=40, ge=1, le=200)
    MAX_REPO_SIZE_MB: int = Field(default=500, ge=10, le=5000)
    MAX_REPO_PYTHON_FILES: int = Field(default=20000, ge=10, le=200000)
    WORKFLOW_TIMEOUT_SECONDS: int = Field(default=7200, ge=60, le=86400)

    @field_validator("CREDENTIAL_ENCRYPTION_KEYS")
    @classmethod
    def validate_fernet_keys(cls, value: str) -> str:
        raw_keys = [k.strip() for k in value.split(",") if k.strip()]
        if not raw_keys:
            raise ValueError("At least one Fernet key must be provided.")
        for idx, key in enumerate(raw_keys):
            try:
                Fernet(key.encode("utf-8"))
            except Exception as exc:
                raise ValueError(
                    f"Invalid Fernet key format at position {idx}."
                ) from exc
        return ",".join(raw_keys)

    @model_validator(mode="after")
    def validate_conditional_integrations(self) -> "Settings":
        if self.APP_ENV == "production":
            if self.APP_SECRET_KEY.startswith("dev-only-") or self.APP_SECRET_KEY.startswith(
                "change-this"
            ):
                raise ValueError(
                    "APP_SECRET_KEY must be changed from placeholder value in production."
                )
            if self.GITHUB_AUTH_MODE == "pat_fallback":
                raise ValueError(
                    "GITHUB_AUTH_MODE='pat_fallback' is only permitted in local development."
                )

        if self.LANGSMITH_TRACING and not self.LANGSMITH_API_KEY.strip():
            raise ValueError(
                "LANGSMITH_API_KEY is required when LANGSMITH_TRACING is enabled."
            )

        if self.GITHUB_AUTH_MODE == "app" and (
            not self.GITHUB_APP_ID.strip() or not self.GITHUB_APP_PRIVATE_KEY_PATH.strip()
        ):
            raise ValueError(
                "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH are required when "
                "GITHUB_AUTH_MODE is 'app'."
            )

        if (
            self.GITHUB_AUTH_MODE == "pat_fallback"
            and not self.GITHUB_PAT_FALLBACK_TOKEN.strip()
        ):
            raise ValueError(
                "GITHUB_PAT_FALLBACK_TOKEN is required when GITHUB_AUTH_MODE is 'pat_fallback'."
            )

        return self

    @property
    def fernet_keys_list(self) -> list[bytes]:
        """Return validated Fernet keys as bytes in priority order."""
        return [k.strip().encode("utf-8") for k in self.CREDENTIAL_ENCRYPTION_KEYS.split(",")]


def load_settings(**overrides: object) -> Settings:
    """Load and validate settings, sanitizing any raw secret values from error messages."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        sanitized_errors: list[dict[str, object]] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ()))
            sanitized_errors.append(
                {
                    "field": loc,
                    "type": err.get("type", "validation_error"),
                    "message": err.get("msg", "Invalid configuration value"),
                }
            )
        raise ConfigurationError(
            "Application configuration validation failed.",
            details={"errors": sanitized_errors},
        ) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton application settings."""
    return load_settings()