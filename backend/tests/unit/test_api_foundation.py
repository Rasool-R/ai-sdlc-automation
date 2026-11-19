"""Unit tests for FastAPI entrypoint, correlation IDs, health endpoints, and Celery setup."""

from collections.abc import Generator

import pytest
from app.api.v1.endpoints import health as health_endpoint
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.schemas.health import ComponentHealth
from app.workflows.celery_app import celery_app
from app.workflows.jobs import execute_worker_ping, ping_worker
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture()
def api_client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    raw_password = "FoundationTestPass!2026"  # noqa: S105
    fernet_key = Fernet.generate_key().decode("utf-8")

    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-111")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password(raw_password))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", fernet_key)
    get_settings.cache_clear()

    application = create_app()
    with TestClient(application) as client:
        yield client

    get_settings.cache_clear()


def test_liveness_endpoint_and_correlation_id_header(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {
        "status": "alive",
        "service": "ai-sdlc-automation",
        "version": "0.1.0",
    }
    assert "X-Correlation-ID" in response.headers
    assert len(response.headers["X-Correlation-ID"]) >= 16

    custom_id = "corr-custom-trace-001"
    echo_resp = api_client.get(
        "/api/v1/health/live",
        headers={"X-Correlation-ID": custom_id},
    )
    assert echo_resp.status_code == 200
    assert echo_resp.headers["X-Correlation-ID"] == custom_id


def test_readiness_requires_authentication(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/health/ready")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_ERROR"


def test_readiness_returns_200_when_dependencies_healthy(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health_endpoint,
        "check_postgres",
        lambda: ComponentHealth(status="ok"),
    )
    monkeypatch.setattr(
        health_endpoint,
        "check_redis",
        lambda: ComponentHealth(status="ok"),
    )
    monkeypatch.setattr(
        health_endpoint,
        "check_celery_broker",
        lambda: ComponentHealth(status="ok"),
    )

    token, _ = create_access_token(subject="admin")
    response = api_client.get(
        "/api/v1/health/ready",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["postgres"]["status"] == "ok"
    assert body["components"]["redis"]["status"] == "ok"
    assert body["components"]["celery_broker"]["status"] == "ok"


def test_readiness_returns_503_when_dependency_fails(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health_endpoint,
        "check_postgres",
        lambda: ComponentHealth(status="error", detail="OperationalError"),
    )
    monkeypatch.setattr(
        health_endpoint,
        "check_redis",
        lambda: ComponentHealth(status="ok"),
    )
    monkeypatch.setattr(
        health_endpoint,
        "check_celery_broker",
        lambda: ComponentHealth(status="ok"),
    )

    token, _ = create_access_token(subject="admin")
    response = api_client.get(
        "/api/v1/health/ready",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["postgres"] == {
        "status": "error",
        "detail": "OperationalError",
    }


def test_celery_app_configuration_and_ping_job() -> None:
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.task_acks_late is True
    assert execute_worker_ping() == {"status": "pong"}
    assert ping_worker.name == "workflows.ping"