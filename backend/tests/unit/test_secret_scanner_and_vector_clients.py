"""Unit tests for outbound secret scanning, data-sharing policy, embeddings, and Qdrant."""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.core.config import load_settings
from app.core.exceptions import AuthorizationError
from app.core.logging import REDACTED_PLACEHOLDER
from app.core.secret_scanner import enforce_data_sharing_policy, scan_outbound_payload
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.qdrant.client import (
    QdrantIndexClient,
    SemanticChunkRecord,
    deterministic_point_id,
)
from cryptography.fernet import Fernet


def test_data_sharing_policy_blocks_private_external_unless_opted_in() -> None:
    # Private repo without opt-in must be denied for external providers
    with pytest.raises(AuthorizationError):
        enforce_data_sharing_policy(
            is_private=True,
            llm_data_sharing=False,
            provider="openai",
            target_purpose="embedding",
        )

    # Private repo with local provider is allowed even when llm_data_sharing=False
    enforce_data_sharing_policy(
        is_private=True,
        llm_data_sharing=False,
        provider="local",
        target_purpose="embedding",
    )

    # Private repo with explicit opt-in is allowed
    enforce_data_sharing_policy(
        is_private=True,
        llm_data_sharing=True,
        provider="openai",
        target_purpose="llm",
    )

    # Public repo is allowed
    enforce_data_sharing_policy(
        is_private=False,
        llm_data_sharing=False,
        provider="openai",
        target_purpose="embedding",
    )


def test_secret_scanner_redacts_and_blocks_without_leaking_values() -> None:
    sample_code = (
        "GITHUB_TOKEN = 'ghp_1234567890abcdefghijklmnopqrstuv'\n"
        "DB_URL = 'postgresql://svc:RealDbSecret99@db:5432/prod'\n"
        "api_key = 'sk-live1234567890abcdefghijklmnop'\n"
        "SAFE_PLACEHOLDER = 'your-api-key-here'\n"
    )

    redacted_result = scan_outbound_payload(sample_code, mode="redact")
    assert redacted_result.has_secrets is True
    assert len(redacted_result.findings) >= 3
    assert "ghp_1234567890abcdefghijklmnopqrstuv" not in redacted_result.sanitized_text
    assert "RealDbSecret99" not in redacted_result.sanitized_text
    assert "sk-live1234567890abcdefghijklmnop" not in redacted_result.sanitized_text
    assert REDACTED_PLACEHOLDER in redacted_result.sanitized_text
    assert "your-api-key-here" in redacted_result.sanitized_text

    with pytest.raises(AuthorizationError) as exc_info:
        scan_outbound_payload(sample_code, mode="block")

    serialized_err = json.dumps(exc_info.value.to_dict())
    assert "RealDbSecret99" not in serialized_err
    assert "ghp_1234567890abcdefghijklmnopqrstuv" not in serialized_err


def test_embedding_client_uses_custom_embedding_base_url() -> None:
    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        EMBEDDING_PROVIDER="local",
        EMBEDDING_MODEL="nomic-embed-text",
        EMBEDDING_BASE_URL="http://localhost:11434/v1/",
        EMBEDDING_API_KEY="local-key-123",  # noqa: S106
    )

    captured_url = ""
    captured_auth = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url, captured_auth
        captured_url = str(request.url)
        captured_auth = request.headers.get("Authorization", "")
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "nomic-embed-text"
        assert body["input"] == ["def foo(): pass", "class Bar: pass"]
        # Return out-of-order indices to verify sorting
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        client = EmbeddingClient(settings=settings, http_client=mock_http)
        assert client.is_enabled is True
        assert client.base_url == "http://localhost:11434/v1"
        vectors = client.embed_texts(["def foo(): pass", "class Bar: pass"])

    assert captured_url == "http://localhost:11434/v1/embeddings"
    assert captured_auth == "Bearer local-key-123"
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


class _FakeQdrantBackend:
    """In-memory stub simulating qdrant-client API for deterministic unit testing."""

    def __init__(self, *, fail_calls: bool = False) -> None:
        self.fail_calls = fail_calls
        self.collections: set[str] = set()
        self.points: dict[str, Any] = {}

    def get_collections(self) -> list[str]:
        if self.fail_calls:
            raise OSError("connection refused")
        return list(self.collections)

    def collection_exists(self, collection_name: str) -> bool:
        if self.fail_calls:
            raise OSError("connection refused")
        return collection_name in self.collections

    def create_collection(self, collection_name: str, **_kwargs: Any) -> None:
        if self.fail_calls:
            raise OSError("connection refused")
        self.collections.add(collection_name)

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        if self.fail_calls:
            raise OSError("connection refused")
        assert collection_name in self.collections
        for pt in points:
            self.points[str(pt.id)] = pt

    def delete(self, collection_name: str, points_selector: Any) -> None:
        if self.fail_calls:
            raise OSError("connection refused")
        assert collection_name in self.collections
        conds = {c.key: c.match for c in points_selector.filter.must}
        target_repo = conds["repository_id"].value
        target_sha = conds["commit_sha"].value
        target_paths = set(conds["file_path"].any)
        to_remove = [
            pid
            for pid, pt in self.points.items()
            if pt.payload["repository_id"] == target_repo
            and pt.payload["commit_sha"] == target_sha
            and pt.payload["file_path"] in target_paths
        ]
        for pid in to_remove:
            del self.points[pid]

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        query_filter: Any,
        limit: int,
        with_payload: bool = True,
    ) -> Any:
        if self.fail_calls:
            raise OSError("connection refused")
        _ = (collection_name, query, with_payload)
        conds = {c.key: c.match.value for c in query_filter.must}
        matched = [
            SimpleNamespace(id=pt.id, payload=pt.payload, score=0.95)
            for pt in self.points.values()
            if pt.payload["repository_id"] == conds["repository_id"]
            and pt.payload["commit_sha"] == conds["commit_sha"]
        ]
        return SimpleNamespace(points=matched[:limit])


def test_qdrant_client_version_isolation_delete_and_fallback() -> None:
    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(CREDENTIAL_ENCRYPTION_KEYS=fernet_key)
    fake_backend = _FakeQdrantBackend()
    client = QdrantIndexClient(settings=settings, client=fake_backend)

    chunk_v1 = SemanticChunkRecord(
        repository_id="repo-1",
        commit_sha="1" * 40,
        symbol_id="app/orders.py::function::app.orders.list_orders",
        qualified_name="app.orders.list_orders",
        kind="function",
        file_path="app/orders.py",
        content_hash="a" * 64,
        start_line=10,
        end_line=25,
        vector=[0.1, 0.2, 0.3],
    )
    chunk_v2 = chunk_v1.model_copy(update={"commit_sha": "2" * 40})

    assert deterministic_point_id("repo-1", "1" * 40, chunk_v1.symbol_id) != (
        deterministic_point_id("repo-1", "2" * 40, chunk_v2.symbol_id)
    )
    assert client.upsert_chunks([chunk_v1, chunk_v2]) is True

    # Querying v1 never leaks v2 records
    hits_v1 = client.search(
        repository_id="repo-1",
        commit_sha="1" * 40,
        query_vector=[0.1, 0.2, 0.3],
    )
    assert len(hits_v1) == 1
    assert hits_v1[0].commit_sha == "1" * 40

    # Stale vector deletion removes only targeted commit/file
    assert client.delete_by_file_paths(
        repository_id="repo-1",
        commit_sha="1" * 40,
        file_paths=["app/orders.py"],
    )
    assert (
        client.search(
            repository_id="repo-1",
            commit_sha="1" * 40,
            query_vector=[0.1, 0.2, 0.3],
        )
        == []
    )
    assert (
        len(
            client.search(
                repository_id="repo-1",
                commit_sha="2" * 40,
                query_vector=[0.1, 0.2, 0.3],
            )
        )
        == 1
    )

    # Graceful fallback when Qdrant is unreachable
    down_client = QdrantIndexClient(
        settings=settings,
        client=_FakeQdrantBackend(fail_calls=True),
    )
    assert down_client.is_available() is False
    assert down_client.upsert_chunks([chunk_v1]) is False
    assert (
        down_client.search(
            repository_id="repo-1",
            commit_sha="1" * 40,
            query_vector=[0.1, 0.2, 0.3],
        )
        == []
    )