"""Integration-style API tests for projects and repositories endpoints with mocked GitHub."""

from collections.abc import Generator

import httpx
import pytest
from app.api.deps import get_github_client
from app.core.config import get_settings, load_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.integrations.github.client import GitHubClient
from app.main import create_app
from app.models import AuditLogModel, Base
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def api_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, sessionmaker[Session], dict[str, str]], None, None]:
    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-222")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password("TestPass!2026"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", fernet_key)
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
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testpat1234567890abcdefghijklmn",  # noqa: S106
    )

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/orders-api":
            return httpx.Response(
                200,
                json={
                    "full_name": "acme/orders-api",
                    "name": "orders-api",
                    "default_branch": "main",
                    "clone_url": "https://github.com/acme/orders-api.git",
                    "private": True,
                    "archived": False,
                    "disabled": False,
                },
            )
        if path == "/repos/acme/orders-api/branches/main":
            return httpx.Response(
                200,
                json={"name": "main", "commit": {"sha": "c" * 40}, "protected": True},
            )
        if path == f"/repos/acme/orders-api/commits/{'c' * 40}":
            return httpx.Response(
                200,
                json={
                    "sha": "c" * 40,
                    "commit": {
                        "message": "Add orders endpoint",
                        "author": {"name": "Bob", "date": "2026-11-21T12:00:00Z"},
                    },
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    mock_http = httpx.Client(transport=httpx.MockTransport(github_handler))

    def override_github_client() -> Generator[GitHubClient, None, None]:
        yield GitHubClient(settings=gh_settings, http_client=mock_http)

    application = create_app()
    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_github_client] = override_github_client

    token, _ = create_access_token(subject="admin")
    auth_headers = {"Authorization": f"Bearer {token}"}

    with TestClient(application) as client:
        yield client, session_factory, auth_headers

    mock_http.close()
    engine.dispose()
    get_settings.cache_clear()


def test_projects_and_repositories_require_authentication(
    api_env: tuple[TestClient, sessionmaker[Session], dict[str, str]],
) -> None:
    client, _sf, _headers = api_env
    assert client.get("/api/v1/projects").status_code == 401
    assert client.post("/api/v1/projects", json={"name": "unauth"}).status_code == 401
    assert client.get("/api/v1/repositories").status_code == 401
    assert (
        client.post(
            "/api/v1/repositories/connect",
            json={"project_id": "p1", "full_name": "acme/orders-api"},
        ).status_code
        == 401
    )


def test_project_crud_lifecycle_and_audit_trail(
    api_env: tuple[TestClient, sessionmaker[Session], dict[str, str]],
) -> None:
    client, session_factory, headers = api_env

    create_resp = client.post(
        "/api/v1/projects",
        json={"name": "payments-core", "description": "FastAPI payments platform"},
        headers=headers,
    )
    assert create_resp.status_code == 201
    project = create_resp.json()
    project_id = project["id"]
    assert project["name"] == "payments-core"

    # Duplicate project name returns 409 Conflict
    dup_resp = client.post(
        "/api/v1/projects",
        json={"name": "payments-core"},
        headers=headers,
    )
    assert dup_resp.status_code == 409

    # List and Get endpoints return persisted project
    list_resp = client.get("/api/v1/projects", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    get_resp = client.get(f"/api/v1/projects/{project_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == project_id

    # Delete removes project and subsequent lookup returns 404
    del_resp = client.delete(f"/api/v1/projects/{project_id}", headers=headers)
    assert del_resp.status_code == 204
    assert client.get(f"/api/v1/projects/{project_id}", headers=headers).status_code == 404

    with session_factory() as db:
        actions = list(db.scalars(select(AuditLogModel.action)).all())
    assert "project.create" in actions
    assert "project.delete" in actions


def test_repository_connect_pins_base_sha_and_enforces_defaults(
    api_env: tuple[TestClient, sessionmaker[Session], dict[str, str]],
) -> None:
    client, session_factory, headers = api_env

    proj_resp = client.post(
        "/api/v1/projects",
        json={"name": "commerce"},
        headers=headers,
    )
    assert proj_resp.status_code == 201
    project_id = proj_resp.json()["id"]

    # Connecting to a non-existent project returns 404
    bad_proj_resp = client.post(
        "/api/v1/repositories/connect",
        json={"project_id": "missing-id", "full_name": "acme/orders-api"},
        headers=headers,
    )
    assert bad_proj_resp.status_code == 404

    # Connecting valid GitHub repository records immutable base commit SHA
    connect_resp = client.post(
        "/api/v1/repositories/connect",
        json={"project_id": project_id, "full_name": "acme/orders-api"},
        headers=headers,
    )
    assert connect_resp.status_code == 201
    repo = connect_resp.json()
    assert repo["full_name"] == "acme/orders-api"
    assert repo["default_branch"] == "main"
    assert repo["is_private"] is True
    assert repo["llm_data_sharing"] is False
    assert "encrypted_credentials" not in repo
    assert repo["latest_version"]["commit_sha"] == "c" * 40
    assert repo["latest_version"]["author_name"] == "Bob"

    # Re-connecting the same repository to the same project returns 409
    dup_repo = client.post(
        "/api/v1/repositories/connect",
        json={"project_id": project_id, "full_name": "acme/orders-api"},
        headers=headers,
    )
    assert dup_repo.status_code == 409

    # Filter list by project_id and retrieve single repository
    list_resp = client.get(
        f"/api/v1/repositories?project_id={project_id}",
        headers=headers,
    )
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    get_resp = client.get(f"/api/v1/repositories/{repo['id']}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["latest_version"]["commit_sha"] == "c" * 40

    with session_factory() as db:
        audit_entry = db.scalar(
            select(AuditLogModel).where(AuditLogModel.action == "repository.connect")
        )
    assert audit_entry is not None
    assert audit_entry.details["base_commit_sha"] == "c" * 40
    assert audit_entry.details["llm_data_sharing"] is False