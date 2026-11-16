"""Structured logging with correlation/workflow IDs and automatic secret redaction."""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="-")
workflow_id_ctx: ContextVar[str] = ContextVar("workflow_id", default="-")

REDACTED_PLACEHOLDER = "[REDACTED]"

_SECRET_KEY_NAMES = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|authorization|private_key|"
    r"credential|fernet|client_secret)",
    re.IGNORECASE,
)

_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer or Basic auth headers
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"(Basic\s+)[A-Za-z0-9+/]+=*", re.IGNORECASE),
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_)
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{16,255}|github_pat_[A-Za-z0-9_]{16,255})\b"),
    # OpenAI / Anthropic / LangSmith style keys (sk-..., lsv2_...)
    re.compile(r"\b(sk-[A-Za-z0-9\-_]{16,255}|lsv2_[A-Za-z0-9\-_]{16,255})\b"),
    # URLs with embedded credentials (e.g., postgresql://user:pass@host)
    re.compile(r"([a-zA-Z0-9+.-]+://[^:\s/@]+:)([^@\s/]+)(@)"),
    # Key-value pairs in free text (e.g., api_key="...", password=...)
    re.compile(
        r"((?:password|secret|token|api_key|authorization|private_key)\s*[:=]\s*['\"]?)"
        r"([^\s,'\";]+)",
        re.IGNORECASE,
    ),
)


def redact_text(text: str, extra_secrets: list[str] | None = None) -> str:
    """Redact known secret patterns and explicit secret strings from text."""
    if not text:
        return text

    sanitized = text
    if extra_secrets:
        for secret in extra_secrets:
            if secret and len(secret) >= 6 and secret in sanitized:
                sanitized = sanitized.replace(secret, REDACTED_PLACEHOLDER)

    sanitized = _SECRET_VALUE_PATTERNS[0].sub(rf"\1{REDACTED_PLACEHOLDER}", sanitized)
    sanitized = _SECRET_VALUE_PATTERNS[1].sub(rf"\1{REDACTED_PLACEHOLDER}", sanitized)
    sanitized = _SECRET_VALUE_PATTERNS[2].sub(REDACTED_PLACEHOLDER, sanitized)
    sanitized = _SECRET_VALUE_PATTERNS[3].sub(REDACTED_PLACEHOLDER, sanitized)
    sanitized = _SECRET_VALUE_PATTERNS[4].sub(rf"\1{REDACTED_PLACEHOLDER}\3", sanitized)
    sanitized = _SECRET_VALUE_PATTERNS[5].sub(rf"\1{REDACTED_PLACEHOLDER}", sanitized)
    return sanitized


def redact_data(data: Any, extra_secrets: list[str] | None = None) -> Any:
    """Recursively redact secrets from dictionaries, lists, tuples, and strings."""
    if isinstance(data, str):
        return redact_text(data, extra_secrets=extra_secrets)
    if isinstance(data, dict):
        sanitized_dict: dict[Any, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and _SECRET_KEY_NAMES.search(key):
                sanitized_dict[key] = REDACTED_PLACEHOLDER
            else:
                sanitized_dict[key] = redact_data(value, extra_secrets=extra_secrets)
        return sanitized_dict
    if isinstance(data, list):
        return [redact_data(item, extra_secrets=extra_secrets) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_data(item, extra_secrets=extra_secrets) for item in data)
    return data


class SecretRedactionFilter(logging.Filter):
    """Logging filter that injects context IDs and scrubs sensitive data."""

    def __init__(self, extra_secrets: list[str] | None = None) -> None:
        super().__init__()
        self.extra_secrets = [s for s in (extra_secrets or []) if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_ctx.get()
        record.workflow_id = workflow_id_ctx.get()

        raw_message = record.getMessage()
        record.msg = redact_text(raw_message, extra_secrets=self.extra_secrets)
        record.args = ()

        if hasattr(record, "structured_data"):
            record.structured_data = redact_data(
                record.structured_data,
                extra_secrets=self.extra_secrets,
            )
        return True


class StructuredJsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects with correlation metadata."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", correlation_id_ctx.get()),
            "workflow_id": getattr(record, "workflow_id", workflow_id_ctx.get()),
        }
        structured_data = getattr(record, "structured_data", None)
        if isinstance(structured_data, dict) and structured_data:
            payload["data"] = structured_data

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            payload["exception"] = redact_text(exc_text)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "json",
    extra_secrets: list[str] | None = None,
) -> None:
    """Configure root logger with structured formatting and secret redaction."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SecretRedactionFilter(extra_secrets=extra_secrets))

    if log_format == "json":
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] [corr=%(correlation_id)s wf=%(workflow_id)s] "
                "%(name)s: %(message)s"
            )
        )

    root_logger.addHandler(handler)