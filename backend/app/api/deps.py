"""Shared FastAPI dependencies for authentication, database sessions, and integrations."""

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AuthenticationError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.github.client import GitHubClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.schemas.auth import OperatorIdentity
from app.services.workspace_service import WorkspaceService
from app.workflows.tasks import CeleryWorkflowDispatcher, WorkflowJobDispatcher

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


def get_workspace_service() -> WorkspaceService:
    """Provide a WorkspaceService bound to configured workspace root and quotas."""
    return WorkspaceService()


def get_embedding_client() -> Generator[EmbeddingClient, None, None]:
    """Provide a request-scoped EmbeddingClient instance and ensure clean shutdown."""
    client = EmbeddingClient()
    try:
        yield client
    finally:
        client.close()


def get_qdrant_client() -> QdrantIndexClient:
    """Provide a QdrantIndexClient configured with application settings."""
    return QdrantIndexClient()


def get_workflow_dispatcher() -> WorkflowJobDispatcher:
    """Provide the background workflow job dispatcher backed by Celery."""
    return CeleryWorkflowDispatcher()


CurrentOperator = Annotated[OperatorIdentity, Depends(get_current_operator)]
DbSession = Annotated[Session, Depends(get_db)]
GitHubClientDep = Annotated[GitHubClient, Depends(get_github_client)]
WorkspaceServiceDep = Annotated[WorkspaceService, Depends(get_workspace_service)]
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client)]
QdrantClientDep = Annotated[QdrantIndexClient, Depends(get_qdrant_client)]
WorkflowDispatcherDep = Annotated[WorkflowJobDispatcher, Depends(get_workflow_dispatcher)]