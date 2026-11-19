"""Unit tests for password hashing, JWT tokens, and operator authentication endpoints."""

from collections.abc import Generator

import pytest
from app.api.v1.endpoints.auth import router as auth_router
from app.core.config import get_settings
from app.core.exceptions import AppError, AuthenticationError
from app.core.security import (
    authenticate_operator,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


@pytest.fixture()
def configured_auth_env(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    raw_password = "StrongOperatorPass!2026"  # noqa: S105
    hashed = hash_password(raw_password)
    fernet_key = Fernet.generate_key().decode("utf-8")

    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-000")
    monkeypatch.setenv("OPERATOR_USERNAME", "ops_admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hashed)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", fernet_key)
    get_settings.cache_clear()
    yield raw_password
    get_settings.cache_clear()


def _build_auth_test_client() -> TestClient:
    test_app = FastAPI()

    async def handle_app_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, AppError)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    test_app.add_exception_handler(AppError, handle_app_error)
    test_app.include_router(auth_router, prefix="/api/v1")
    return TestClient(test_app)


def test_password_hashing_and_verification() -> None:
    secret = "CorrectHorseBatteryStaple!42"  # noqa: S105
    hashed = hash_password(secret)

    assert hashed != secret
    assert verify_password(secret, hashed) is True
    assert verify_password("wrong-password", hashed) is False
    assert verify_password(secret, "malformed-hash") is False


def test_jwt_create_decode_and_expiry(configured_auth_env: str) -> None:
    _ = configured_auth_env
    token, expires_in = create_access_token(subject="ops_admin", expires_minutes=15)
    assert expires_in == 900

    claims = decode_access_token(token)
    assert claims.sub == "ops_admin"
    assert claims.token_type == "access"  # noqa: S105

    expired_token, _ = create_access_token(subject="ops_admin", expires_minutes=-1)
    with pytest.raises(AuthenticationError, match="expired"):
        decode_access_token(expired_token)

    with pytest.raises(AuthenticationError, match="Invalid"):
        decode_access_token(f"{token}tampered")


def test_authenticate_operator_direct(configured_auth_env: str) -> None:
    assert authenticate_operator("ops_admin", configured_auth_env) == "ops_admin"

    with pytest.raises(AuthenticationError):
        authenticate_operator("wrong_user", configured_auth_env)

    with pytest.raises(AuthenticationError):
        authenticate_operator("ops_admin", "wrong_pass")


def test_auth_endpoints_login_and_protected_route(configured_auth_env: str) -> None:
    client = _build_auth_test_client()

    # Unauthenticated access to protected route must fail with 401
    unauth_resp = client.get("/api/v1/auth/me")
    assert unauth_resp.status_code == 401

    # Invalid login credentials must fail with 401
    bad_login = client.post(
        "/api/v1/auth/login",
        json={"username": "ops_admin", "password": "invalid"},
    )
    assert bad_login.status_code == 401

    # Valid login returns Bearer token
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "ops_admin", "password": configured_auth_env},
    )
    assert login_resp.status_code == 200
    body = login_resp.json()
    assert body["token_type"] == "bearer"  # noqa: S105
    assert body["expires_in"] > 0

    # Protected route succeeds with valid Bearer token
    me_resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json() == {"username": "ops_admin", "role": "operator"}

    # Protected route rejects invalid token
    bad_token_resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    assert bad_token_resp.status_code == 401