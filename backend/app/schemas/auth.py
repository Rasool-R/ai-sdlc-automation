"""Authentication request, response, and operator identity schemas."""

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Credentials payload submitted by the operator to obtain an access token."""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """Bearer access token response returned after successful authentication."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(ge=1)


class OperatorIdentity(BaseModel):
    """Authenticated single-operator identity attached to protected requests."""

    username: str
    role: str = "operator"


class TokenClaims(BaseModel):
    """Validated JWT claims extracted from an operator access token."""

    model_config = ConfigDict(populate_by_name=True)

    sub: str = Field(min_length=1)
    token_type: str = Field(alias="type")
    exp: int
    iat: int