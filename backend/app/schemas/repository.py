"""Pydantic schemas for connecting repositories and tracking immutable base versions."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RepositoryConnectRequest(BaseModel):
    """Request payload for connecting an authorized remote repository."""

    project_id: str = Field(min_length=1, max_length=36)
    full_name: str = Field(
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        description="Repository identifier in 'owner/repo' format.",
    )
    provider: Literal["github"] = "github"
    default_branch: str | None = Field(default=None, min_length=1, max_length=100)
    github_installation_id: str | None = Field(default=None, max_length=64)
    llm_data_sharing: bool = False


class RepositoryVersionRead(BaseModel):
    """Serialized immutable base commit snapshot for a repository."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    repository_id: str
    branch_name: str
    commit_sha: str
    commit_message: str | None
    author_name: str | None
    committed_at: datetime | None
    created_at: datetime


class RepositoryRead(BaseModel):
    """Serialized connected repository metadata (never exposing raw credentials)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    full_name: str
    provider: str
    clone_url: str
    default_branch: str
    github_installation_id: str | None
    is_private: bool
    llm_data_sharing: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    latest_version: RepositoryVersionRead | None = None


class RepositoryListResponse(BaseModel):
    """Paginated list of connected repositories."""

    items: list[RepositoryRead]
    total: int