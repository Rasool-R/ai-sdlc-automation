"""Pluggable OpenAI-compatible embedding client supporting EMBEDDING_BASE_URL."""

from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, ConfigurationError

DEFAULT_OPENAI_EMBEDDING_BASE_URL = "https://api.openai.com/v1"


class EmbeddingClient:
    """Generates dense vector embeddings via hosted OpenAI or custom/local endpoints."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    @property
    def provider(self) -> str:
        """Return configured embedding provider name."""
        return self._settings.EMBEDDING_PROVIDER

    @property
    def model(self) -> str:
        """Return configured embedding model identifier."""
        return self._settings.EMBEDDING_MODEL

    @property
    def base_url(self) -> str:
        """Resolve effective embedding endpoint base URL using EMBEDDING_BASE_URL."""
        configured = self._settings.EMBEDDING_BASE_URL.strip().rstrip("/")
        if configured:
            return configured
        if self._settings.EMBEDDING_PROVIDER == "openai":
            return DEFAULT_OPENAI_EMBEDDING_BASE_URL
        return ""

    @property
    def is_enabled(self) -> bool:
        """Return True when semantic embedding generation is configured and enabled."""
        if self._settings.EMBEDDING_PROVIDER == "disabled":
            return False
        if self._settings.EMBEDDING_PROVIDER == "local":
            return bool(self.base_url)
        return bool(self._settings.EMBEDDING_API_KEY.strip() or self._settings.EMBEDDING_BASE_URL)

    def close(self) -> None:
        """Close underlying HTTP client if owned by this instance."""
        if self._owns_client:
            self._http.close()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate vector embeddings for an ordered batch of input texts."""
        if not texts:
            return []

        if self._settings.EMBEDDING_PROVIDER == "disabled":
            raise ConfigurationError("Embedding provider is disabled.")

        resolved_base_url = self.base_url
        if not resolved_base_url:
            raise ConfigurationError(
                "EMBEDDING_BASE_URL must be configured for the selected embedding provider."
            )

        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = self._settings.EMBEDDING_API_KEY.strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        elif self._settings.EMBEDDING_PROVIDER=="openai" and not self._settings.EMBEDDING_BASE_URL:
            raise ConfigurationError(
                "EMBEDDING_API_KEY is required when using the default OpenAI embedding endpoint."
            )

        endpoint = f"{resolved_base_url}/embeddings"
        request_body: dict[str, Any] = {
            "model": self._settings.EMBEDDING_MODEL,
            "input": texts,
        }

        try:
            response = self._http.post(endpoint, headers=headers, json=request_body)
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error communicating with embedding endpoint.",
                code="EMBEDDING_CONNECTION_ERROR",
                status_code=502,
                details={"provider": self.provider, "reason": type(exc).__name__},
            ) from exc

        if response.status_code >= 400:
            raise AppError(
                f"Embedding endpoint returned HTTP {response.status_code}.",
                code="EMBEDDING_API_ERROR",
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
                "Embedding endpoint returned invalid JSON.",
                code="EMBEDDING_INVALID_RESPONSE",
                status_code=502,
                details={"provider": self.provider},
            ) from exc

        data_items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data_items, list) or len(data_items) != len(texts):
            raise AppError(
                "Embedding response count did not match requested input batch size.",
                code="EMBEDDING_INVALID_RESPONSE",
                status_code=502,
                details={"expected": len(texts)},
            )

        indexed_vectors: list[tuple[int, list[float]]] = []
        for fallback_idx, item in enumerate(data_items):
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise AppError(
                    "Malformed vector item in embedding response.",
                    code="EMBEDDING_INVALID_RESPONSE",
                    status_code=502,
                )
            raw_index = item.get("index", fallback_idx)
            idx = int(raw_index) if isinstance(raw_index, int) else fallback_idx
            vector = [float(v) for v in item["embedding"]]
            indexed_vectors.append((idx, vector))

        indexed_vectors.sort(key=lambda pair: pair[0])
        return [vec for _, vec in indexed_vectors]