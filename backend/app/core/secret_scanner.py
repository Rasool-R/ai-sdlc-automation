"""Outbound secret scanner and private-repository data-sharing policy enforcement."""

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.core.exceptions import AuthorizationError
from app.core.logging import REDACTED_PLACEHOLDER

_SAFE_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "example",
    "placeholder",
    "changeme",
    "change-this",
    "your-",
    "your_",
    "<",
    "[redacted]",
    "dummy",
    "test_",
)


class SecretFinding(BaseModel):
    """Secret-safe record describing a detected sensitive value without storing raw data."""

    rule_id: str = Field(min_length=1)
    line: int = Field(ge=1)
    action: Literal["redacted", "blocked"]


class SecretScanResult(BaseModel):
    """Outcome of scanning an outbound text payload before external transmission."""

    has_secrets: bool = False
    findings: list[SecretFinding] = Field(default_factory=list)
    sanitized_text: str


_SECRET_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        False,
    ),
    (
        "aws_access_key_id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        False,
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,255}|github_pat_[A-Za-z0-9_]{16,255})\b"),
        False,
    ),
    (
        "provider_api_key",
        re.compile(r"\b(?:sk-[A-Za-z0-9\-_]{16,255}|lsv2_[A-Za-z0-9\-_]{16,255})\b"),
        False,
    ),
    (
        "url_embedded_credentials",
        re.compile(r"([a-zA-Z0-9+.-]+://[^:\s/@]+:)([^@\s/]{4,})(@)"),
        True,
    ),
    (
        "generic_secret_assignment",
        re.compile(
            r"(\b(?:api_key|apikey|secret_key|client_secret|access_token|"
            r"private_key|password|passwd|db_password)\b\s*[:=]\s*['\"])([^'\"\n]{8,})(['\"])",
            re.IGNORECASE,
        ),
        True,
    ),
)


def _line_number_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_obvious_placeholder(candidate: str) -> bool:
    lowered = candidate.strip().lower()
    if not lowered or lowered == REDACTED_PLACEHOLDER.lower():
        return True
    return any(lowered.startswith(prefix) for prefix in _SAFE_PLACEHOLDER_PREFIXES)


def enforce_data_sharing_policy(
    *,
    is_private: bool,
    llm_data_sharing: bool,
    provider: str,
    target_purpose: Literal["llm", "embedding", "tracing"],
) -> None:
    """Enforce per-repository opt-in before transmitting private repository code externally."""
    if not is_private:
        return

    normalized_provider = provider.strip().lower()
    if normalized_provider in {"local", "disabled"}:
        return

    if not llm_data_sharing:
        raise AuthorizationError(
            f"External {target_purpose} transmission is denied for private repositories "
            "unless llm_data_sharing is explicitly enabled or a local provider is configured.",
            details={
                "target_purpose": target_purpose,
                "provider": normalized_provider,
                "llm_data_sharing": llm_data_sharing,
            },
        )


def scan_outbound_payload(
    text: str,
    *,
    mode: Literal["redact", "block"] = "redact",
) -> SecretScanResult:
    """Scan outbound text for hardcoded secrets and redact or block before transmission."""
    if not text:
        return SecretScanResult(has_secrets=False, findings=[], sanitized_text=text)

    findings: list[SecretFinding] = []
    sanitized = text

    for rule_id, pattern, has_groups in _SECRET_RULES:
        if has_groups:
            def _replace_grouped(
                match: re.Match[str],
                *,
                current_rule: str = rule_id,
                source_text: str = sanitized,
            ) -> str:
                secret_val = match.group(2)
                if _is_obvious_placeholder(secret_val):
                    return match.group(0)
                findings.append(
                    SecretFinding(
                        rule_id=current_rule,
                        line=_line_number_at(source_text, match.start()),
                        action="blocked" if mode == "block" else "redacted",
                    )
                )
                return f"{match.group(1)}{REDACTED_PLACEHOLDER}{match.group(3)}"

            sanitized = pattern.sub(_replace_grouped, sanitized)
        else:
            def _replace_whole(
                match: re.Match[str],
                *,
                current_rule: str = rule_id,
                source_text: str = sanitized,
            ) -> str:
                findings.append(
                    SecretFinding(
                        rule_id=current_rule,
                        line=_line_number_at(source_text, match.start()),
                        action="blocked" if mode == "block" else "redacted",
                    )
                )
                return REDACTED_PLACEHOLDER

            sanitized = pattern.sub(_replace_whole, sanitized)

    if mode == "block" and findings:
        raise AuthorizationError(
            "Outbound payload blocked due to detected secrets.",
            details={
                "findings_count": len(findings),
                "rules": sorted({f.rule_id for f in findings}),
            },
        )

    return SecretScanResult(
        has_secrets=bool(findings),
        findings=findings,
        sanitized_text=sanitized,
    )