"""Pydantic schemas for connecting, indexing, and inspecting repositories."""

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


class RepositoryIndexRequest(BaseModel):
    """Request payload for triggering version-pinned repository indexing."""

    branch: str | None = Field(default=None, min_length=1, max_length=100)
    commit_sha: str | None = Field(default=None, min_length=7, max_length=64)
    force_reindex: bool = False


class RepositoryIndexResponse(BaseModel):
    """Summary returned after indexing a repository snapshot."""

    snapshot_id: str
    repository_id: str
    commit_sha: str
    status: str
    files_indexed_count: int = Field(ge=0)
    symbols_count: int = Field(ge=0)
    relationships_count: int = Field(ge=0)
    parse_errors_count: int = Field(ge=0)
    semantic_indexed: bool
    reused_snapshot: bool
    unchanged_files_skipped: int = Field(ge=0)
    reindexed_files: int = Field(ge=0)
    deleted_files: int = Field(ge=0)
    secrets_redacted_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class IndexedSymbolRead(BaseModel):
    """Serialized structural code symbol with exact version provenance."""

    model_config = ConfigDict(from_attributes=True)

    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    commit_sha: str
    content_hash: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    is_test: bool


class IndexedRelationshipRead(BaseModel):
    """Serialized structural or dependency relationship edge."""

    model_config = ConfigDict(from_attributes=True)

    source_symbol: str
    target_symbol: str
    relationship_type: str
    confidence: str
    file_path: str
    line: int | None


class RepositoryStructureResponse(BaseModel):
    """Versioned structural overview returned by the repository structure endpoint."""

    repository_id: str
    snapshot_id: str
    commit_sha: str
    files_indexed_count: int = Field(ge=0)
    semantic_indexed: bool
    symbols: list[IndexedSymbolRead]
    relationships: list[IndexedRelationshipRead]
    total_symbols: int = Field(ge=0)
    total_relationships: int = Field(ge=0)