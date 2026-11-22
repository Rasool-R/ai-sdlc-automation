"""Version-isolated Qdrant semantic index client with structural-only fallback support."""

import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_CODE_COLLECTION = "code_symbols_v1"


class SemanticChunkRecord(BaseModel):
    """Logical code unit embedded and stored in Qdrant with version provenance."""

    repository_id: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7, max_length=64)
    symbol_id: str = Field(min_length=1)
    qualified_name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    is_test: bool = False
    vector: list[float] = Field(min_length=1)


class VectorSearchHit(BaseModel):
    """Single semantic retrieval hit strictly tied to a repository version."""

    symbol_id: str
    qualified_name: str
    kind: str
    file_path: str
    commit_sha: str
    content_hash: str
    start_line: int
    end_line: int
    is_test: bool = False
    score: float


def deterministic_point_id(repository_id: str, commit_sha: str, symbol_id: str) -> str:
    """Derive a deterministic UUIDv5 point ID for idempotent Qdrant upserts."""
    raw = f"{repository_id}:{commit_sha}:{symbol_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


class QdrantIndexClient:
    """Wraps Qdrant operations with version filtering and graceful degradation."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        collection_name: str = DEFAULT_CODE_COLLECTION,
        client: Any | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        self._settings = settings or get_settings()
        self._collection_name = collection_name
        if client is not None:
            self._client = client
        else:
            api_key = self._settings.QDRANT_API_KEY.strip() or None
            self._client = QdrantClient(
                url=self._settings.QDRANT_URL,
                api_key=api_key,
                timeout=timeout_seconds,
            )

    @property
    def collection_name(self) -> str:
        """Return target Qdrant collection name."""
        return self._collection_name

    def is_available(self) -> bool:
        """Return True when Qdrant responds to a lightweight collection probe."""
        try:
            self._client.get_collections()
            return True
        except (UnexpectedResponse, OSError, ValueError, RuntimeError):
            return False

    def ensure_collection(self, vector_size: int) -> bool:
        """Create the Qdrant collection if missing; return False if Qdrant is unreachable."""
        try:
            if not self._client.collection_exists(self._collection_name):
                self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=qmodels.VectorParams(
                        size=vector_size,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
            return True
        except (UnexpectedResponse, OSError, ValueError, RuntimeError) as exc:
            logger.warning(
                "Qdrant unavailable while ensuring collection; falling back to structural mode",
                extra={"structured_data": {"reason": type(exc).__name__}},
            )
            return False

    def upsert_chunks(self, chunks: list[SemanticChunkRecord]) -> bool:
        """Upsert embedded logical code chunks idempotently into Qdrant."""
        if not chunks:
            return True

        vector_size = len(chunks[0].vector)
        if not self.ensure_collection(vector_size):
            return False

        points = [
            qmodels.PointStruct(
                id=deterministic_point_id(c.repository_id, c.commit_sha, c.symbol_id),
                vector=c.vector,
                payload={
                    "repository_id": c.repository_id,
                    "commit_sha": c.commit_sha,
                    "symbol_id": c.symbol_id,
                    "qualified_name": c.qualified_name,
                    "kind": c.kind,
                    "file_path": c.file_path,
                    "content_hash": c.content_hash,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "is_test": c.is_test,
                },
            )
            for c in chunks
        ]

        try:
            self._client.upsert(collection_name=self._collection_name, points=points)
            return True
        except (UnexpectedResponse, OSError, ValueError, RuntimeError) as exc:
            logger.warning(
                "Qdrant upsert failed; structural index remains authoritative",
                extra={"structured_data": {"reason": type(exc).__name__}},
            )
            return False

    def delete_by_file_paths(
        self,
        *,
        repository_id: str,
        commit_sha: str,
        file_paths: list[str],
    ) -> bool:
        """Remove stale vectors for modified or deleted files within a repository version."""
        if not file_paths:
            return True

        try:
            if not self._client.collection_exists(self._collection_name):
                return True
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="repository_id",
                        match=qmodels.MatchValue(value=repository_id),
                    ),
                    qmodels.FieldCondition(
                        key="commit_sha",
                        match=qmodels.MatchValue(value=commit_sha),
                    ),
                    qmodels.FieldCondition(
                        key="file_path",
                        match=qmodels.MatchAny(any=file_paths),
                    ),
                ]
            )
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=qmodels.FilterSelector(filter=query_filter),
            )
            return True
        except (UnexpectedResponse, OSError, ValueError, RuntimeError) as exc:
            logger.warning(
                "Qdrant stale vector cleanup failed",
                extra={"structured_data": {"reason": type(exc).__name__}},
            )
            return False

    def search(
        self,
        *,
        repository_id: str,
        commit_sha: str,
        query_vector: list[float],
        limit: int = 20,
    ) -> list[VectorSearchHit]:
        """Search vectors strictly scoped to (repository_id, commit_sha) without version mixing."""
        if not query_vector:
            return []

        try:
            if not self._client.collection_exists(self._collection_name):
                return []

            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="repository_id",
                        match=qmodels.MatchValue(value=repository_id),
                    ),
                    qmodels.FieldCondition(
                        key="commit_sha",
                        match=qmodels.MatchValue(value=commit_sha),
                    ),
                ]
            )
            response = self._client.query_points(
                collection_name=self._collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        except (UnexpectedResponse, OSError, ValueError, RuntimeError) as exc:
            logger.warning(
                "Qdrant semantic search unavailable; returning empty semantic set for fallback",
                extra={"structured_data": {"reason": type(exc).__name__}},
            )
            return []

        raw_points = getattr(response, "points", [])
        hits: list[VectorSearchHit] = []
        for point in raw_points:
            payload = getattr(point, "payload", None)
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("repository_id") != repository_id
                or payload.get("commit_sha") != commit_sha
            ):
                continue
            hits.append(
                VectorSearchHit(
                    symbol_id=str(payload["symbol_id"]),
                    qualified_name=str(payload["qualified_name"]),
                    kind=str(payload["kind"]),
                    file_path=str(payload["file_path"]),
                    commit_sha=str(payload["commit_sha"]),
                    content_hash=str(payload["content_hash"]),
                    start_line=int(payload["start_line"]),
                    end_line=int(payload["end_line"]),
                    is_test=bool(payload.get("is_test", False)),
                    score=float(getattr(point, "score", 0.0)),
                )
            )
        return hits