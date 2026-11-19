"""Shared FastAPI dependencies for authentication and request context."""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import AuthenticationError
from app.core.security import decode_access_token
from app.schemas.auth import OperatorIdentity

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_operator(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> OperatorIdentity:
    """Validate Bearer JWT and return the authenticated operator identity."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing or invalid authorization header")

    claims = decode_access_token(credentials.credentials)
    return OperatorIdentity(username=claims.sub)


CurrentOperator = Annotated[OperatorIdentity, Depends(get_current_operator)]