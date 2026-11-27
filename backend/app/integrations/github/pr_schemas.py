"""Pydantic v2 schemas for controlled GitHub Pull Request, review, and CI status operations."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CIStatusState = Literal["success", "failure", "pending", "unknown"]
ReviewDecisionState = Literal["approved", "changes_requested", "commented", "pending"]


class GitHubPullRequestInfo(BaseModel):
    """Normalized Pull Request metadata returned by the GitHub REST API."""

    number: int = Field(ge=1)
    title: str
    body: str = ""
    state: str
    draft: bool = False
    html_url: str
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    merged: bool = False


class GitHubReviewSummary(BaseModel):
    """Normalized Pull Request review entry."""

    reviewer: str
    state: str
    submitted_at: datetime | None = None


class GitHubCheckRunSummary(BaseModel):
    """Normalized CI check run or commit status entry."""

    name: str
    status: str
    conclusion: str | None = None
    details_url: str | None = None


class GitHubPullRequestStatusSnapshot(BaseModel):
    """Aggregated on-demand PR, review, and CI status retrieved from GitHub."""

    pull_request: GitHubPullRequestInfo
    ci_status: CIStatusState = "unknown"
    review_decision: ReviewDecisionState = "pending"
    checks: list[GitHubCheckRunSummary] = Field(default_factory=list)
    reviews: list[GitHubReviewSummary] = Field(default_factory=list)