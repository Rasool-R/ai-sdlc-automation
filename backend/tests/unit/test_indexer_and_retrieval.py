"""Unit tests for incremental indexing, privacy/fallback behavior, hybrid retrieval, and API."""

import json
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.api.deps import (
    get_embedding_client,
    get_github_client,
    get_qdrant_client,
    get_workspace_service,
)
from app.core.config import get_settings, load_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.github.client import GitHubClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.indexer import RepositoryIndexer
from app.intelligence.retrieval import HybridRetrievalService
from app.main import create_app
from app.models import Base, ProjectModel, RepositoryModel
from app.services.workspace_service import WorkspaceService
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from git import Repo
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


class _MemoryQdrant:
    """In-memory Qdrant backend stub for deterministic hybrid retrieval tests."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.collections: set[str] = set()
        self.points: dict[str, Any] = {}
        self.deleted_batches: list[list[str]] = []

    def get_collections(self) -> list[str]:
        if not self.available:
            raise OSError("qdrant unreachable")
        return list(self.collections)

    def collection_exists(self, collection_name: str) -> bool:
        if not self.available:
            raise OSError("qdrant unreachable")
        return collection_name in self.collections

    def create_collection(self, collection_name: str, **_kwargs: Any) -> None:
        if not self.available:
            raise OSError("qdrant unreachable")
        self.collections.add(collection_name)

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        if not self.available:
            raise OSError("qdrant unreachable")
        assert collection_name in self.collections
        for pt in points:
            self.points[str(pt.id)] = pt

    def delete(self, collection_name: str, points_selector: Any) -> None:
        if not self.available:
            raise OSError("qdrant unreachable")
        assert collection_name in self.collections
        conds = {c.key: c.match for c in points_selector.filter.must}
        repo_id = conds["repository_id"].value
        sha = conds["commit_sha"].value
        paths = list(conds["file_path"].any)
        self.deleted_batches.append(paths)
        stale_ids = [
            pid
            for pid, pt in self.points.items()
            if pt.payload["repository_id"] == repo_id
            and pt.payload["commit_sha"] == sha
            and pt.payload["file_path"] in paths
        ]
        for pid in stale_ids:
            del self.points[pid]

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        query_filter: Any,
        limit: int,
        with_payload: bool = True,
    ) -> Any:
        if not self.available:
            raise OSError("qdrant unreachable")
        _ = (collection_name, query, with_payload)
        conds = {c.key: c.match.value for c in query_filter.must}
        hits = []
        for pt in self.points.values():
            if (
                pt.payload["repository_id"] == conds["repository_id"]
                and pt.payload["commit_sha"] == conds["commit_sha"]
            ):
                score = 0.92 if "billing" in pt.payload["qualified_name"] else 0.45
                hits.append(SimpleNamespace(id=pt.id, payload=pt.payload, score=score))
        hits.sort(key=lambda h: -h.score)
        return SimpleNamespace(points=hits[:limit])


def _write_repo_file(base: Path, rel_path: str, content: str) -> None:
    target = base / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_repo(
    db: Session,
    *,
    is_private: bool = True,
    llm_data_sharing: bool = False,
) -> RepositoryModel:
    project = ProjectModel(name="intel-project", description="Intelligence tests")
    db.add(project)
    db.flush()
    repo = RepositoryModel(
        project_id=project.id,
        name="orders-api",
        full_name="acme/orders-api",
        provider="github",
        clone_url="https://github.com/acme/orders-api.git",
        default_branch="main",
        is_private=is_private,
        llm_data_sharing=llm_data_sharing,
    )
    db.add(repo)
    db.flush()
    return repo


def test_incremental_indexing_redaction_and_hybrid_retrieval(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo = _seed_repo(db_session, is_private=True, llm_data_sharing=False)
    workspace = tmp_path / "ws"
    _write_repo_file(
        workspace,
        "app/orders.py",
        (
            "class OrderRepository:\n"
            '    """Retrieves orders from storage."""\n'
            "    def list_orders(self) -> list[dict[str, str]]:\n"
            "        api_key = 'sk-live1234567890abcdefghijklmnop'\n"
            "        return [{'status': api_key}]\n"
        ),
    )
    _write_repo_file(
        workspace,
        "app/billing.py",
        (
            "def compute_invoice_total(items: list[int]) -> int:\n"
            '    """Calculate total charge for customer checkout."""\n'
            "    return sum(items)\n"
        ),
    )
    _write_repo_file(
        workspace,
        "tests/test_orders.py",
        (
            "from app.orders import OrderRepository\n\n"
            "def test_list_orders() -> None:\n"
            "    assert isinstance(OrderRepository().list_orders(), list)\n"
        ),
    )

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        EMBEDDING_PROVIDER="local",
        EMBEDDING_MODEL="nomic-embed-text",
        EMBEDDING_BASE_URL="http://localhost:11434/v1",
    )

    embedded_payloads: list[str] = []

    def embed_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        embedded_payloads.extend(inputs)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": idx, "embedding": [0.1, 0.2, 0.3]}
                    for idx in range(len(inputs))
                ]
            },
        )

    fake_qdrant = _MemoryQdrant(available=True)
    with httpx.Client(transport=httpx.MockTransport(embed_handler)) as mock_http:
        embed_client = EmbeddingClient(settings=settings, http_client=mock_http)
        qdrant_client = QdrantIndexClient(settings=settings, client=fake_qdrant)
        indexer = RepositoryIndexer(
            db_session,
            settings=settings,
            embedding_client=embed_client,
            qdrant_client=qdrant_client,
        )

        sha_v1 = "1" * 40
        res_v1 = indexer.index_workspace(
            repository=repo,
            workspace_dir=workspace,
            commit_sha=sha_v1,
        )
        assert res_v1.semantic_indexed is True
        assert res_v1.reused_snapshot is False
        assert res_v1.files_indexed_count == 3
        assert res_v1.secrets_redacted_count >= 1
        assert all("sk-live1234567890abcdefghijklmnop" not in text for text in embedded_payloads)

        res_v1_repeat = indexer.index_workspace(
            repository=repo,
            workspace_dir=workspace,
            commit_sha=sha_v1,
        )
        assert res_v1_repeat.reused_snapshot is True
        assert res_v1_repeat.snapshot_id == res_v1.snapshot_id

        _write_repo_file(
            workspace,
            "app/orders.py",
            (
                "class OrderRepository:\n"
                '    """Retrieves paginated orders."""\n'
                "    def list_orders(self, limit: int = 20) -> list[dict[str, str]]:\n"
                "        return [{'limit': str(limit)}]\n"
            ),
        )
        (workspace / "app/billing.py").unlink()

        sha_v2 = "2" * 40
        res_v2 = indexer.index_workspace(
            repository=repo,
            workspace_dir=workspace,
            commit_sha=sha_v2,
        )
        assert res_v2.reused_snapshot is False
        assert res_v2.unchanged_files_skipped == 1
        assert res_v2.reindexed_files == 1
        assert res_v2.deleted_files == 1

        retriever = HybridRetrievalService(
            db_session,
            settings=settings,
            embedding_client=embed_client,
            qdrant_client=qdrant_client,
        )
        retrieval_v1 = retriever.retrieve(
            repository=repo,
            commit_sha=sha_v1,
            query="Update list_orders in app/orders.py and checkout charge",
            workspace_dir=workspace,
            max_tokens=2000,
            max_files=5,
        )
        assert retrieval_v1.retrieval_mode == "hybrid"
        assert all(item.commit_sha == sha_v1 for item in retrieval_v1.items)
        qnames_v1 = {item.qualified_name for item in retrieval_v1.items}
        assert "app.orders.OrderRepository.list_orders" in qnames_v1
        assert "app.billing.compute_invoice_total" in qnames_v1
        assert retrieval_v1.total_estimated_tokens <= 2000

        tiny_retrieval = retriever.retrieve(
            repository=repo,
            commit_sha=sha_v1,
            query="list_orders in app/orders.py",
            workspace_dir=workspace,
            max_tokens=80,
            max_files=1,
        )
        assert tiny_retrieval.files_included <= 1
        assert tiny_retrieval.total_estimated_tokens <= 80
        assert tiny_retrieval.truncated is True


def test_private_repo_external_embedding_falls_back_to_structural_only(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo = _seed_repo(db_session, is_private=True, llm_data_sharing=False)
    workspace = tmp_path / "ws_private"
    _write_repo_file(
        workspace,
        "app/items.py",
        "def fetch_items() -> list[str]:\n    return ['a']\n",
    )

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        EMBEDDING_PROVIDER="openai",
        EMBEDDING_API_KEY="sk-should-never-be-called-1234567890",  # noqa: S106
    )

    external_called = False

    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        nonlocal external_called
        external_called = True
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(fail_if_called)) as mock_http:
        embed_client = EmbeddingClient(settings=settings, http_client=mock_http)
        qdrant_client = QdrantIndexClient(
            settings=settings,
            client=_MemoryQdrant(available=True),
        )

        indexer = RepositoryIndexer(
            db_session,
            settings=settings,
            embedding_client=embed_client,
            qdrant_client=qdrant_client,
        )
        sha = "3" * 40
        res = indexer.index_workspace(repository=repo, workspace_dir=workspace, commit_sha=sha)
        assert external_called is False
        assert res.semantic_indexed is False
        assert res.symbols_count >= 2
        assert any("data-sharing" in w for w in res.warnings)

        retriever = HybridRetrievalService(
            db_session,
            settings=settings,
            embedding_client=embed_client,
            qdrant_client=qdrant_client,
        )
        retrieval = retriever.retrieve(
            repository=repo,
            commit_sha=sha,
            query="fetch_items",
            workspace_dir=workspace,
        )
        assert external_called is False
        assert retrieval.retrieval_mode == "structural_only"
        assert any(item.name == "fetch_items" for item in retrieval.items)


def test_index_and_structure_api_endpoints_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_repo_dir = tmp_path / "origin_repo"
    source_repo_dir.mkdir()
    git_repo = Repo.init(str(source_repo_dir))
    try:
        with git_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Tester")
            cfg.set_value("user", "email", "tester@example.com")
        _write_repo_file(
            source_repo_dir,
            "app/service.py",
            "def list_widgets() -> list[str]:\n    return ['w1']\n",
        )
        git_repo.git.add(A=True)
        commit_sha = str(git_repo.index.commit("Initial widgets").hexsha)
        default_branch = str(git_repo.active_branch.name)
    finally:
        git_repo.close()

    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-333")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password("ApiTestPass!2026"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", fernet_key)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "disabled")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    gh_settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        GITHUB_AUTH_MODE="pat_fallback",
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testtoken1234567890abcdefghij",  # noqa: S106
        WORKSPACE_ROOT_DIR=tmp_path / "managed_ws",
        EMBEDDING_PROVIDER="disabled",
    )

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/widgets":
            return httpx.Response(
                200,
                json={
                    "full_name": "acme/widgets",
                    "name": "widgets",
                    "default_branch": default_branch,
                    "clone_url": "https://github.com/acme/widgets.git",
                    "private": True,
                },
            )
        if path == f"/repos/acme/widgets/branches/{default_branch}":
            return httpx.Response(
                200,
                json={"name": default_branch, "commit": {"sha": commit_sha}, "protected": False},
            )
        if path == f"/repos/acme/widgets/commits/{commit_sha}":
            return httpx.Response(
                200,
                json={
                    "sha": commit_sha,
                    "commit": {
                        "message": "Initial widgets",
                        "author": {"name": "Tester", "date": "2026-11-22T10:00:00Z"},
                    },
                },
            )
        return httpx.Response(404)

    mock_http = httpx.Client(transport=httpx.MockTransport(github_handler))

    class _LocalCloneGitHubClient(GitHubClient):
        def build_authenticated_clone_url(
            self,
            clone_url: str,
            *,
            installation_id: str | None = None,
        ) -> str:
            _ = (clone_url, installation_id)
            return str(source_repo_dir)

    def override_github() -> Generator[GitHubClient, None, None]:
        yield _LocalCloneGitHubClient(settings=gh_settings, http_client=mock_http)

    def override_workspace() -> WorkspaceService:
        return WorkspaceService(settings=gh_settings)

    def override_embeddings() -> Generator[EmbeddingClient, None, None]:
        yield EmbeddingClient(settings=gh_settings)

    def override_qdrant() -> QdrantIndexClient:
        return QdrantIndexClient(settings=gh_settings, client=_MemoryQdrant(available=False))

    application = create_app()
    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_github_client] = override_github
    application.dependency_overrides[get_workspace_service] = override_workspace
    application.dependency_overrides[get_embedding_client] = override_embeddings
    application.dependency_overrides[get_qdrant_client] = override_qdrant

    token, _ = create_access_token(subject="admin")
    headers = {"Authorization": f"Bearer {token}"}

    try:
        with TestClient(application) as client:
            proj = client.post(
                "/api/v1/projects",
                json={"name": "widgets-platform"},
                headers=headers,
            ).json()
            repo = client.post(
                "/api/v1/repositories/connect",
                json={"project_id": proj["id"], "full_name": "acme/widgets"},
                headers=headers,
            ).json()

            assert (
                client.get(
                    f"/api/v1/repositories/{repo['id']}/structure",
                    headers=headers,
                ).status_code
                == 404
            )

            index_resp = client.post(
                f"/api/v1/repositories/{repo['id']}/index",
                json={},
                headers=headers,
            )
            assert index_resp.status_code == 200
            idx_body = index_resp.json()
            assert idx_body["commit_sha"] == commit_sha
            assert idx_body["files_indexed_count"] == 1
            assert idx_body["symbols_count"] == 2
            assert idx_body["semantic_indexed"] is False

            assert list((tmp_path / "managed_ws").iterdir()) == []

            struct_resp = client.get(
                f"/api/v1/repositories/{repo['id']}/structure?kind=function",
                headers=headers,
            )
            assert struct_resp.status_code == 200
            struct_body = struct_resp.json()
            assert struct_body["commit_sha"] == commit_sha
            assert struct_body["total_symbols"] == 1
            assert struct_body["symbols"][0]["name"] == "list_widgets"
    finally:
        mock_http.close()
        engine.dispose()
        get_settings.cache_clear()