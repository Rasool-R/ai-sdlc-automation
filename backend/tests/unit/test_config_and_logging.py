"""Unit tests for configuration validation, secret redaction, and structured logging."""

import json
import logging
from io import StringIO

import pytest
from app.core.config import load_settings
from app.core.exceptions import ConfigurationError
from app.core.logging import (
    REDACTED_PLACEHOLDER,
    SecretRedactionFilter,
    StructuredJsonFormatter,
    correlation_id_ctx,
    redact_data,
    redact_text,
    workflow_id_ctx,
)
from cryptography.fernet import Fernet


def test_valid_default_settings() -> None:
    valid_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(CREDENTIAL_ENCRYPTION_KEYS=valid_key)
    assert settings.APP_NAME == "ai-sdlc-automation"
    assert len(settings.fernet_keys_list) == 1
    assert settings.MAX_CORRECTION_LOOPS_PER_FAILURE == 3
    assert settings.MAX_LLM_CALLS_PER_WORKFLOW == 150


def test_invalid_fernet_key_raises_secret_safe_configuration_error() -> None:
    bad_secret_value = "not-a-valid-fernet-key-secret-value-12345"  # noqa: S105
    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(CREDENTIAL_ENCRYPTION_KEYS=bad_secret_value)

    error_dict = exc_info.value.to_dict()
    serialized = json.dumps(error_dict)
    assert bad_secret_value not in serialized
    assert error_dict["error"]["code"] == "CONFIGURATION_ERROR"


def test_production_rejects_placeholder_secret_and_pat_fallback() -> None:
    valid_key = Fernet.generate_key().decode("utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(
            APP_ENV="production",
            APP_SECRET_KEY="dev-only-secret-key-at-least-32-characters-long",  # noqa: S106
            CREDENTIAL_ENCRYPTION_KEYS=valid_key,
        )

    with pytest.raises(ConfigurationError):
        load_settings(
            APP_ENV="production",
            APP_SECRET_KEY="prod-strong-random-key-with-more-than-32-chars",  # noqa: S106
            CREDENTIAL_ENCRYPTION_KEYS=valid_key,
            GITHUB_AUTH_MODE="pat_fallback",
            GITHUB_PAT_FALLBACK_TOKEN="ghp_1234567890abcdefghijklmnopqrstuv",  # noqa: S106
        )


def test_redact_text_and_nested_data() -> None:
    raw_msg = (
        "Connecting to postgresql+psycopg://admin:supersecretdbpass@db:5432/app "
        "with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig "
        "and github_pat_1234567890abcdefghijklmnopqrstuv "
        "and sk-proj1234567890abcdefghijklmnop"
    )
    scrubbed = redact_text(raw_msg)
    assert "supersecretdbpass" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in scrubbed
    assert "github_pat_1234567890abcdefghijklmnopqrstuv" not in scrubbed
    assert "sk-proj1234567890abcdefghijklmnop" not in scrubbed
    assert REDACTED_PLACEHOLDER in scrubbed

    nested = {
        "user": "admin",
        "api_key": "raw-api-key-value",
        "nested": {"client_secret": "top-secret", "safe_field": "ok"},
    }
    scrubbed_dict = redact_data(nested)
    assert scrubbed_dict["api_key"] == REDACTED_PLACEHOLDER
    assert scrubbed_dict["nested"]["client_secret"] == REDACTED_PLACEHOLDER
    assert scrubbed_dict["nested"]["safe_field"] == "ok"


def test_structured_json_logger_injects_context_and_redacts_secrets() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter(extra_secrets=["custom-sensitive-token-999"]))
    handler.setFormatter(StructuredJsonFormatter())

    test_logger = logging.getLogger("test.security.logger")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    corr_token = correlation_id_ctx.set("req-12345")
    wf_token = workflow_id_ctx.set("wf-67890")
    try:
        test_logger.info(
            "Using token custom-sensitive-token-999 and api_key=my_secret_key_123",
            extra={"structured_data": {"authorization": "Bearer secret-jwt", "step": "init"}},
        )
    finally:
        correlation_id_ctx.reset(corr_token)
        workflow_id_ctx.reset(wf_token)

    output = stream.getvalue().strip()
    parsed = json.loads(output)
    assert parsed["correlation_id"] == "req-12345"
    assert parsed["workflow_id"] == "wf-67890"
    assert "custom-sensitive-token-999" not in output
    assert "my_secret_key_123" not in output
    assert "secret-jwt" not in output
    assert parsed["data"]["authorization"] == REDACTED_PLACEHOLDER
    assert parsed["data"]["step"] == "init"