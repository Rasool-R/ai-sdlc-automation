"""Shared FastAPI dependencies for authentication, database sessions, and integrations."""

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AuthenticationError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.integrations.github.client import GitHubClient
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


def get_github_client() -> Generator[GitHubClient, None, None]:
    """Provide a request-scoped GitHubClient instance and ensure clean shutdown."""
    client = GitHubClient()
    try:
        yield client
    finally:
        client.close()


CurrentOperator = Annotated[OperatorIdentity, Depends(get_current_operator)]
DbSession = Annotated[Session, Depends(get_db)]
GitHubClientDep = Annotated[GitHubClient, Depends(get_github_client)]