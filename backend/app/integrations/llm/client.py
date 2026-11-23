"""OpenAI-compatible LLM client with structured Pydantic outputs, retries, and redaction."""

import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, ConfigurationError, ValidationFailedError
from app.core.secret_scanner import enforce_data_sharing_policy, scan_outbound_payload

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

DEFAULT_OPENAI_LLM_BASE_URL = "https://api.openai.com/v1"
_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

# Approximate USD rates per 1M tokens (input, output) for common hosted models.
# Unknown or self-hosted models report cost_status="unavailable" rather than fabricating numbers.
_MODEL_PRICING_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
}


class LLMUsageMetrics(BaseModel):
    """Per-call telemetry capturing token usage, latency, retries, and cost provenance."""

    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: Decimal | None = None
    cost_status: Literal["estimated", "unavailable"] = "unavailable"
    latency_seconds: float = Field(default=0.0, ge=0.0)
    retry_count: int = Field(default=0, ge=0)
    repair_attempts: int = Field(default=0, ge=0)
    secrets_redacted: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class StructuredLLMResponse(Generic[TModel]):
    """Validated Pydantic agent output paired with its call telemetry."""

    parsed: TModel
    usage: LLMUsageMetrics
    raw_text: str


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> tuple[Decimal | None, Literal["estimated", "unavailable"]]:
    """Compute estimated USD cost when rates and token counts are known; never fabricate."""
    if input_tokens is None or output_tokens is None:
        return None, "unavailable"

    rates = _MODEL_PRICING_PER_MILLION.get(model.strip().lower())
    if rates is None:
        return None, "unavailable"

    in_rate, out_rate = rates
    million = Decimal("1000000")
    cost = (Decimal(input_tokens) * in_rate + Decimal(output_tokens) * out_rate) / million
    return cost.quantize(Decimal("0.000001")), "estimated"


def _extract_json_text(raw_content: str) -> str:
    stripped = raw_content.strip()
    fence_match = _JSON_FENCE_PATTERN.search(stripped)
    if fence_match:
        return fence_match.group(1).strip()

    start_idx = stripped.find("{")
    end_idx = stripped.rfind("}")
    if start_idx != -1 and end_idx > start_idx:
        return stripped[start_idx : end_idx + 1]
    return stripped


def _backoff_with_jitter(
    attempt: int,
    base_seconds: float = 0.5,
    cap_seconds: float = 10.0,
) -> float:
    multiplier = 1 << max(0, attempt)
    exp_delay = min(cap_seconds, base_seconds * multiplier)
    jitter_fraction = secrets.randbelow(250) / 1000.0
    return exp_delay + jitter_fraction


class LLMClient:
    """Controlled LLM client enforcing privacy policy, secret redaction, and schema contracts."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        max_repair_attempts: int = 2,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self._max_retries = max_retries
        self._max_repair_attempts = max_repair_attempts
        self._sleep = sleep_fn

    @property
    def provider(self) -> str:
        """Return configured LLM provider name."""
        return self._settings.LLM_PROVIDER

    @property
    def default_model(self) -> str:
        """Return configured default model identifier."""
        return self._settings.LLM_MODEL_DEFAULT

    @property
    def base_url(self) -> str:
        """Resolve effective OpenAI-compatible chat completions base URL."""
        configured = self._settings.LLM_BASE_URL.strip().rstrip("/")
        if configured:
            return configured
        if self._settings.LLM_PROVIDER == "openai":
            return DEFAULT_OPENAI_LLM_BASE_URL
        return ""

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._http.close()

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._settings.LLM_API_KEY.strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        elif self._settings.LLM_PROVIDER == "openai" and not self._settings.LLM_BASE_URL.strip():
            raise ConfigurationError(
                "LLM_API_KEY is required when using the default OpenAI endpoint."
            )
        return headers

    def _post_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
    ) -> tuple[str, int | None, int | None, int | None, int]:
        resolved_base = self.base_url
        if not resolved_base:
            raise ConfigurationError(
                "LLM_BASE_URL must be configured for non-default or local LLM providers."
            )

        endpoint = f"{resolved_base}/chat/completions"
        headers = self._build_headers()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        retries_used = 0
        last_error: AppError | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.post(endpoint, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = AppError(
                    "Transient network error communicating with LLM endpoint.",
                    code="LLM_CONNECTION_ERROR",
                    status_code=502,
                    details={"provider": self.provider, "reason": type(exc).__name__},
                )
                if attempt < self._max_retries:
                    retries_used += 1
                    self._sleep(_backoff_with_jitter(attempt))
                    continue
                raise last_error from exc

            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = AppError(
                    f"LLM endpoint returned retryable HTTP {response.status_code}.",
                    code="LLM_UPSTREAM_RETRYABLE",
                    status_code=502,
                    details={
                        "provider": self.provider,
                        "upstream_status": response.status_code,
                    },
                )
                if attempt < self._max_retries:
                    retries_used += 1
                    self._sleep(_backoff_with_jitter(attempt))
                    continue
                raise last_error

            if response.status_code >= 400:
                raise AppError(
                    f"LLM endpoint returned HTTP {response.status_code}.",
                    code="LLM_API_ERROR",
                    status_code=502,
                    details={
                        "provider": self.provider,
                        "upstream_status": response.status_code,
                    },
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise AppError(
                    "LLM endpoint returned invalid JSON envelope.",
                    code="LLM_INVALID_RESPONSE",
                    status_code=502,
                    details={"provider": self.provider},
                ) from exc

            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices:
                raise AppError(
                    "LLM response contained no choices.",
                    code="LLM_INVALID_RESPONSE",
                    status_code=502,
                )

            first_choice = choices[0]
            message_obj = first_choice.get("message") if isinstance(first_choice, dict) else None
            content = message_obj.get("content") if isinstance(message_obj, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise AppError(
                    "LLM response message content was empty.",
                    code="LLM_EMPTY_RESPONSE",
                    status_code=502,
                )

            usage_obj = payload.get("usage") if isinstance(payload, dict) else None
            prompt_tokens: int | None = None
            completion_tokens: int | None = None
            total_tokens: int | None = None
            if isinstance(usage_obj, dict):
                raw_in = usage_obj.get("prompt_tokens")
                raw_out = usage_obj.get("completion_tokens")
                raw_tot = usage_obj.get("total_tokens")
                if isinstance(raw_in, int) and raw_in >= 0:
                    prompt_tokens = raw_in
                if isinstance(raw_out, int) and raw_out >= 0:
                    completion_tokens = raw_out
                if isinstance(raw_tot, int) and raw_tot >= 0:
                    total_tokens = raw_tot
                elif prompt_tokens is not None and completion_tokens is not None:
                    total_tokens = prompt_tokens + completion_tokens

            return content, prompt_tokens, completion_tokens, total_tokens, retries_used

        assert last_error is not None
        raise last_error

    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[TModel],
        prompt_version: str,
        is_private_repo: bool,
        llm_data_sharing: bool,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> StructuredLLMResponse[TModel]:
        """Request JSON conforming to response_model with bounded schema repair."""
        enforce_data_sharing_policy(
            is_private=is_private_repo,
            llm_data_sharing=llm_data_sharing,
            provider=self.provider,
            target_purpose="llm",
        )

        sys_scan = scan_outbound_payload(system_prompt, mode="redact")
        usr_scan = scan_outbound_payload(user_prompt, mode="redact")
        secrets_redacted = len(sys_scan.findings) + len(usr_scan.findings)

        schema_json = json.dumps(response_model.model_json_schema(), sort_keys=True)
        effective_system = (
            f"{sys_scan.sanitized_text}\n\n"
            "OUTPUT CONTRACT:\n"
            "Return ONLY a single valid JSON object matching this exact JSON Schema. "
            "Do not include markdown commentary outside the JSON object.\n"
            f"JSON Schema:\n{schema_json}"
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": effective_system},
            {"role": "user", "content": usr_scan.sanitized_text},
        ]

        resolved_model = (model or self.default_model).strip()
        start_time = time.monotonic()

        total_in = 0
        total_out = 0
        has_in_tokens = False
        has_out_tokens = False
        total_retries = 0
        repair_attempts = 0
        last_raw = ""
        last_validation_msg = ""

        for repair_idx in range(self._max_repair_attempts + 1):
            raw_content, in_tok, out_tok, _tot_tok, call_retries = self._post_chat_completion(
                model=resolved_model,
                messages=messages,
                temperature=temperature,
            )
            last_raw = raw_content
            total_retries += call_retries
            if in_tok is not None:
                total_in += in_tok
                has_in_tokens = True
            if out_tok is not None:
                total_out += out_tok
                has_out_tokens = True

            extracted = _extract_json_text(raw_content)
            try:
                parsed_obj = json.loads(extracted)
                validated = response_model.model_validate(parsed_obj)
                latency = round(max(0.0, time.monotonic() - start_time), 4)
                final_in = total_in if has_in_tokens else None
                final_out = total_out if has_out_tokens else None
                final_total = (
                    (final_in + final_out)
                    if final_in is not None and final_out is not None
                    else None
                )
                est_cost, cost_status = estimate_cost_usd(
                    model=resolved_model,
                    input_tokens=final_in,
                    output_tokens=final_out,
                )
                usage = LLMUsageMetrics(
                    provider=self.provider,
                    model=resolved_model,
                    prompt_version=prompt_version,
                    input_tokens=final_in,
                    output_tokens=final_out,
                    total_tokens=final_total,
                    estimated_cost_usd=est_cost,
                    cost_status=cost_status,
                    latency_seconds=latency,
                    retry_count=total_retries,
                    repair_attempts=repair_attempts,
                    secrets_redacted=secrets_redacted,
                )
                return StructuredLLMResponse(parsed=validated, usage=usage, raw_text=raw_content)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_validation_msg = str(exc)
                if repair_idx >= self._max_repair_attempts:
                    break
                repair_attempts += 1
                messages.append({"role": "assistant", "content": raw_content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous JSON output failed schema validation:\n"
                            f"{last_validation_msg[:1500]}\n\n"
                            "Return ONLY a corrected valid JSON object conforming to the schema."
                        ),
                    }
                )

        raise ValidationFailedError(
            f"LLM output failed schema validation after {repair_attempts} repair attempt(s).",
            details={
                "response_model": response_model.__name__,
                "prompt_version": prompt_version,
                "repair_attempts": repair_attempts,
                "validation_error": last_validation_msg[:500],
                "raw_preview_length": len(last_raw),
            },
        )