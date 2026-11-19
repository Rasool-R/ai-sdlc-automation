"""Password hashing, JWT token lifecycle, and single-operator authentication."""

import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.schemas.auth import TokenClaims

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password using bcrypt with a fresh salt."""
    if not plain_password:
        raise ValueError("Password must not be empty.")
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    if not plain_password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except ValueError:
        return False


def authenticate_operator(username: str, password: str) -> str:
    """Validate single-operator credentials using constant-time comparison."""
    settings = get_settings()
    username_matches = hmac.compare_digest(username, settings.OPERATOR_USERNAME)
    password_matches = verify_password(password, settings.OPERATOR_PASSWORD_HASH)

    if not (username_matches and password_matches):
        raise AuthenticationError("Invalid username or password")

    return settings.OPERATOR_USERNAME


def create_access_token(
    subject: str,
    *,
    expires_minutes: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Create a signed JWT access token and return (token, expires_in_seconds)."""
    settings = get_settings()
    ttl_minutes = (
        expires_minutes if expires_minutes is not None else settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    now = datetime.now(UTC)
    expires_delta = timedelta(minutes=ttl_minutes)
    expire_at = now + expires_delta

    payload: dict[str, Any] = {
        "sub": subject,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(now.timestamp()),
        "exp": int(expire_at.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)

    encoded = jwt.encode(payload, settings.APP_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded, int(expires_delta.total_seconds())


def decode_access_token(token: str) -> TokenClaims:
    """Decode and validate a signed JWT access token."""
    settings = get_settings()
    try:
        raw_payload = jwt.decode(
            token,
            settings.APP_SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token has expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid access token") from exc

    try:
        claims = TokenClaims.model_validate(raw_payload)
    except ValidationError as exc:
        raise AuthenticationError("Malformed access token claims") from exc

    if claims.token_type != ACCESS_TOKEN_TYPE:
        raise AuthenticationError("Unexpected token type")

    return claims