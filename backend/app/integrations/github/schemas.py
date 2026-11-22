"""Validated Pydantic schemas for GitHub repository, branch, commit, and file metadata."""

from datetime import datetime

from pydantic import BaseModel, Field


class GitHubRepositoryInfo(BaseModel):
    """Metadata describing an authorized GitHub repository."""

    full_name: str = Field(min_length=3)
    name: str = Field(min_length=1)
    default_branch: str = Field(min_length=1)
    clone_url: str = Field(min_length=8)
    is_private: bool = True
    archived: bool = False
    disabled: bool = False


class GitHubBranchInfo(BaseModel):
    """Metadata describing a branch head and its exact commit SHA."""

    name: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7, max_length=64)
    protected: bool = False


class GitHubCommitInfo(BaseModel):
    """Metadata describing a specific GitHub commit."""

    sha: str = Field(min_length=7, max_length=64)
    message: str = ""
    author_name: str | None = None
    committed_at: datetime | None = None


class GitHubFileEntry(BaseModel):
    """Metadata describing a file or directory entry inside a repository tree."""

    path: str = Field(min_length=1)
    entry_type: str = Field(min_length=1)
    sha: str = Field(min_length=7, max_length=64)
    size: int = Field(default=0, ge=0)