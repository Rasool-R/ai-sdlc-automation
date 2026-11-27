"""Controlled GitHub REST API client supporting GitHub App auth and local PAT fallback.

This service strictly enforces least-privilege access and contains no merge or deployment paths.
"""

import base64
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import jwt

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from app.integrations.github.pr_schemas import (
    CIStatusState,
    GitHubCheckRunSummary,
    GitHubPullRequestInfo,
    GitHubPullRequestStatusSnapshot,
    GitHubReviewSummary,
    ReviewDecisionState,
)
from app.integrations.github.schemas import (
    GitHubBranchInfo,
    GitHubCommitInfo,
    GitHubFileEntry,
    GitHubRepositoryInfo,
)

logger = logging.getLogger(__name__)

GITHUB_API_VERSION = "2022-11-28"
DEFAULT_GITHUB_API_BASE = "https://api.github.com"

_FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
_SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})


def _parse_pr_dict(data: dict[str, Any]) -> GitHubPullRequestInfo:
    raw_head = data.get("head")
    head: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
    raw_base = data.get("base")
    base: dict[str, Any] = raw_base if isinstance(raw_base, dict) else {}
    return GitHubPullRequestInfo(
        number=int(data["number"]),
        title=str(data.get("title", "")),
        body=str(data.get("body") or ""),
        state=str(data.get("state", "open")),
        draft=bool(data.get("draft", False)),
        html_url=str(data.get("html_url", "")),
        head_branch=str(head.get("ref", "")),
        head_sha=str(head.get("sha", "")),
        base_branch=str(base.get("ref", "")),
        base_sha=str(base.get("sha", "")),
        merged=bool(data.get("merged", False)),
    )


def _derive_review_decision(reviews: list[GitHubReviewSummary]) -> ReviewDecisionState:
    latest_by_reviewer: dict[str, str] = {}
    for item in reviews:
        state_upper = item.state.strip().upper()
        if state_upper in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}:
            latest_by_reviewer[item.reviewer] = state_upper

    states = set(latest_by_reviewer.values())
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    if "COMMENTED" in states:
        return "commented"
    return "pending"


def _derive_ci_status(
    checks: list[GitHubCheckRunSummary],
    combined_status_state: str | None,
) -> CIStatusState:
    if checks:
        if any((c.conclusion or "").lower() in _FAILURE_CONCLUSIONS for c in checks):
            return "failure"
        if any(c.status.lower() != "completed" for c in checks):
            return "pending"
        if all((c.conclusion or "").lower() in _SUCCESS_CONCLUSIONS for c in checks):
            return "success"

    if combined_status_state:
        lower = combined_status_state.strip().lower()
        if lower in {"failure", "error"}:
            return "failure"
        if lower == "pending":
            return "pending"
        if lower == "success":
            return "success"

    return "unknown"


class GitHubClient:
    """Controlled GitHub REST API client for repository inspection and PR preparation.

    Intentionally exposes NO pull-request merge, auto-merge, or deployment methods.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        api_base_url: str = DEFAULT_GITHUB_API_BASE,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._api_base_url = api_base_url.rstrip("/")
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            base_url=self._api_base_url,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self._cached_installation_tokens: dict[str, tuple[str, float]] = {}

    def close(self) -> None:
        """Close the underlying HTTP client when owned by this instance."""
        if self._owns_client:
            self._http.close()

    def _validate_full_name(self, full_name: str) -> tuple[str, str]:
        parts = full_name.strip().split("/")
        if len(parts) != 2 or not all(parts) or ".." in parts:
            raise ValidationFailedError(
                "Repository name must follow 'owner/repo' format.",
                details={"full_name": full_name},
            )
        return parts[0], parts[1]

    def _create_app_jwt(self) -> str:
        """Create a short-lived RS256 JWT signed by the GitHub App private key."""
        app_id = self._settings.GITHUB_APP_ID.strip()
        key_path_str = self._settings.GITHUB_APP_PRIVATE_KEY_PATH.strip()
        if not app_id or not key_path_str:
            raise ConfigurationError(
                "GitHub App authentication requires GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH."
            )

        key_path = Path(key_path_str)
        try:
            private_key_pem = key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(
                "Unable to read GitHub App private key file.",
                details={"reason": type(exc).__name__},
            ) from exc

        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": app_id,
        }
        try:
            return jwt.encode(payload, private_key_pem, algorithm="RS256")
        except (jwt.PyJWTError, ValueError) as exc:
            raise ConfigurationError(
                "Failed to sign GitHub App JWT with configured private key.",
                details={"reason": type(exc).__name__},
            ) from exc

    def _get_installation_token(self, installation_id: str | None = None) -> str:
        """Resolve an access token using configured auth mode (App or local PAT fallback)."""
        auth_mode = self._settings.GITHUB_AUTH_MODE
        if auth_mode == "disabled":
            raise ConfigurationError(
                "GitHub integration is disabled. Configure GITHUB_AUTH_MODE='app' "
                "(or 'pat_fallback' in local development)."
            )

        if auth_mode == "pat_fallback":
            pat = self._settings.GITHUB_PAT_FALLBACK_TOKEN.strip()
            if not pat:
                raise ConfigurationError("GITHUB_PAT_FALLBACK_TOKEN is not configured.")
            return pat

        resolved_install_id = (
            installation_id or self._settings.GITHUB_APP_INSTALLATION_ID
        ).strip()
        if not resolved_install_id:
            raise ConfigurationError(
                "A GitHub App installation ID is required to request an installation token."
            )

        cached = self._cached_installation_tokens.get(resolved_install_id)
        if cached is not None and cached[1] > time.time() + 60:
            return cached[0]

        app_jwt = self._create_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        url = f"{self._api_base_url}/app/installations/{quote(resolved_install_id)}/access_tokens"
        try:
            response = self._http.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error while requesting GitHub App installation token.",
                code="GITHUB_CONNECTION_ERROR",
                status_code=502,
                details={"reason": type(exc).__name__},
            ) from exc

        data = self._parse_json_response(response, resource="github_installation")
        token_value = str(data.get("token", "")).strip()
        if not token_value:
            raise AuthenticationError("GitHub installation token response did not include a token.")

        expires_at_raw = data.get("expires_at")
        expires_ts = time.time() + 3000
        if isinstance(expires_at_raw, str):
            try:
                expires_ts = datetime.fromisoformat(
                    expires_at_raw.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                expires_ts = time.time() + 3000

        self._cached_installation_tokens[resolved_install_id] = (token_value, expires_ts)
        return token_value

    def _request_headers(self, installation_id: str | None = None) -> dict[str, str]:
        access_token = self._get_installation_token(installation_id=installation_id)
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def _parse_json_any(
        self,
        response: httpx.Response,
        *,
        resource: str,
    ) -> Any:
        if response.status_code == 401:
            raise AuthenticationError(
                "GitHub rejected authentication credentials.",
                details={"resource": resource},
            )
        if response.status_code == 403:
            raise AuthorizationError(
                "GitHub denied access to the requested resource or rate limit was exceeded.",
                details={"resource": resource},
            )
        if response.status_code == 404:
            raise ResourceNotFoundError(
                f"GitHub resource not found: {resource}.",
                details={"resource": resource},
            )
        if response.status_code >= 400:
            raise AppError(
                f"GitHub API returned HTTP {response.status_code}.",
                code="GITHUB_API_ERROR",
                status_code=502,
                details={"resource": resource, "upstream_status": response.status_code},
            )

        try:
            return response.json()
        except ValueError as exc:
            raise AppError(
                "GitHub API returned invalid JSON payload.",
                code="GITHUB_INVALID_RESPONSE",
                status_code=502,
                details={"resource": resource},
            ) from exc

    def _parse_json_response(
        self,
        response: httpx.Response,
        *,
        resource: str,
    ) -> dict[str, Any]:
        payload = self._parse_json_any(response, resource=resource)
        if not isinstance(payload, dict):
            raise AppError(
                "Unexpected GitHub API JSON structure.",
                code="GITHUB_INVALID_RESPONSE",
                status_code=502,
                details={"resource": resource},
            )
        return payload

    def _parse_json_list(
        self,
        response: httpx.Response,
        *,
        resource: str,
    ) -> list[dict[str, Any]]:
        payload = self._parse_json_any(response, resource=resource)
        if not isinstance(payload, list):
            raise AppError(
                "Unexpected GitHub API JSON list structure.",
                code="GITHUB_INVALID_RESPONSE",
                status_code=502,
                details={"resource": resource},
            )
        return [item for item in payload if isinstance(item, dict)]

    def _get(
        self,
        path: str,
        *,
        resource: str,
        installation_id: str | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = self._request_headers(installation_id=installation_id)
        url = f"{self._api_base_url}{path}"
        try:
            response = self._http.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error communicating with GitHub API.",
                code="GITHUB_CONNECTION_ERROR",
                status_code=502,
                details={"resource": resource, "reason": type(exc).__name__},
            ) from exc
        return self._parse_json_response(response, resource=resource)

    def _get_list(
        self,
        path: str,
        *,
        resource: str,
        installation_id: str | None = None,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        headers = self._request_headers(installation_id=installation_id)
        url = f"{self._api_base_url}{path}"
        try:
            response = self._http.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error communicating with GitHub API.",
                code="GITHUB_CONNECTION_ERROR",
                status_code=502,
                details={"resource": resource, "reason": type(exc).__name__},
            ) from exc
        return self._parse_json_list(response, resource=resource)

    def _post(
        self,
        path: str,
        *,
        resource: str,
        json_body: dict[str, Any],
        installation_id: str | None = None,
    ) -> dict[str, Any]:
        headers = self._request_headers(installation_id=installation_id)
        url = f"{self._api_base_url}{path}"
        try:
            response = self._http.post(url, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            raise AppError(
                "Network error communicating with GitHub API.",
                code="GITHUB_CONNECTION_ERROR",
                status_code=502,
                details={"resource": resource, "reason": type(exc).__name__},
            ) from exc
        return self._parse_json_response(response, resource=resource)

    def get_repository(
        self,
        full_name: str,
        *,
        installation_id: str | None = None,
    ) -> GitHubRepositoryInfo:
        """Retrieve authorized repository metadata from GitHub."""
        owner, repo = self._validate_full_name(full_name)
        data = self._get(
            f"/repos/{quote(owner)}/{quote(repo)}",
            resource=full_name,
            installation_id=installation_id,
        )
        return GitHubRepositoryInfo(
            full_name=str(data["full_name"]),
            name=str(data["name"]),
            default_branch=str(data.get("default_branch", "main")),
            clone_url=str(data["clone_url"]),
            is_private=bool(data.get("private", True)),
            archived=bool(data.get("archived", False)),
            disabled=bool(data.get("disabled", False)),
        )

    def get_branch(
        self,
        full_name: str,
        branch_name: str,
        *,
        installation_id: str | None = None,
    ) -> GitHubBranchInfo:
        """Retrieve branch head metadata including its exact commit SHA."""
        owner, repo = self._validate_full_name(full_name)
        data = self._get(
            f"/repos/{quote(owner)}/{quote(repo)}/branches/{quote(branch_name, safe='')}",
            resource=f"{full_name}@{branch_name}",
            installation_id=installation_id,
        )
        raw_commit = data.get("commit")
        commit_obj: dict[str, Any] = raw_commit if isinstance(raw_commit, dict) else {}
        commit_sha = str(commit_obj.get("sha", ""))
        return GitHubBranchInfo(
            name=str(data["name"]),
            commit_sha=commit_sha,
            protected=bool(data.get("protected", False)),
        )

    def get_commit(
        self,
        full_name: str,
        commit_sha: str,
        *,
        installation_id: str | None = None,
    ) -> GitHubCommitInfo:
        """Retrieve commit metadata for a specific immutable SHA."""
        owner, repo = self._validate_full_name(full_name)
        data = self._get(
            f"/repos/{quote(owner)}/{quote(repo)}/commits/{quote(commit_sha)}",
            resource=f"{full_name}@{commit_sha}",
            installation_id=installation_id,
        )
        commit_meta = data.get("commit", {})
        message = ""
        author_name: str | None = None
        committed_at: datetime | None = None
        if isinstance(commit_meta, dict):
            message = str(commit_meta.get("message", ""))
            author = commit_meta.get("author", {})
            if isinstance(author, dict):
                raw_name = author.get("name")
                author_name = str(raw_name) if raw_name is not None else None
                raw_date = author.get("date")
                if isinstance(raw_date, str):
                    try:
                        committed_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    except ValueError:
                        committed_at = None

        return GitHubCommitInfo(
            sha=str(data["sha"]),
            message=message,
            author_name=author_name,
            committed_at=committed_at,
        )

    def list_tree_files(
        self,
        full_name: str,
        commit_sha: str,
        *,
        installation_id: str | None = None,
    ) -> list[GitHubFileEntry]:
        """Retrieve the recursive file tree at a pinned commit SHA."""
        owner, repo = self._validate_full_name(full_name)
        data = self._get(
            f"/repos/{quote(owner)}/{quote(repo)}/git/trees/{quote(commit_sha)}",
            resource=f"{full_name}@{commit_sha}:tree",
            installation_id=installation_id,
            params={"recursive": "1"},
        )
        raw_tree = data.get("tree", [])
        if not isinstance(raw_tree, list):
            return []

        entries: list[GitHubFileEntry] = []
        for item in raw_tree:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            entry_type = str(item.get("type", "")).strip()
            sha = str(item.get("sha", "")).strip()
            size = int(item.get("size", 0) or 0)
            if path and entry_type and sha:
                entries.append(
                    GitHubFileEntry(path=path, entry_type=entry_type, sha=sha, size=size)
                )
        return entries

    def get_file_content(
        self,
        full_name: str,
        file_path: str,
        *,
        ref: str,
        installation_id: str | None = None,
    ) -> str:
        """Retrieve decoded UTF-8 file content at a specific ref/commit SHA."""
        owner, repo = self._validate_full_name(full_name)
        normalized_path = file_path.lstrip("/")
        if not normalized_path or ".." in Path(normalized_path).parts:
            raise ValidationFailedError(
                "Invalid repository file path.",
                details={"file_path": file_path},
            )

        data = self._get(
            f"/repos/{quote(owner)}/{quote(repo)}/contents/{quote(normalized_path, safe='/')}",
            resource=f"{full_name}:{normalized_path}@{ref}",
            installation_id=installation_id,
            params={"ref": ref},
        )
        encoding = str(data.get("encoding", ""))
        raw_content = str(data.get("content", ""))
        if encoding != "base64":
            raise ValidationFailedError(
                "Unsupported file encoding returned by GitHub.",
                details={"encoding": encoding, "file_path": normalized_path},
            )
        try:
            decoded_bytes = base64.b64decode(raw_content)
            return decoded_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationFailedError(
                "File is binary or not valid UTF-8 text.",
                details={"file_path": normalized_path},
            ) from exc

    def find_open_pull_request(
        self,
        full_name: str,
        *,
        head_branch: str,
        base_branch: str,
        installation_id: str | None = None,
    ) -> GitHubPullRequestInfo | None:
        """Check whether an open Pull Request already exists for (head_branch -> base_branch)."""
        owner, repo = self._validate_full_name(full_name)
        items = self._get_list(
            f"/repos/{quote(owner)}/{quote(repo)}/pulls",
            resource=f"{full_name}:pulls:{head_branch}",
            installation_id=installation_id,
            params={
                "state": "open",
                "head": f"{owner}:{head_branch}",
                "base": base_branch,
            },
        )
        if not items:
            return None
        return _parse_pr_dict(items[0])

    def create_pull_request(
        self,
        full_name: str,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        draft: bool = False,
        installation_id: str | None = None,
    ) -> tuple[GitHubPullRequestInfo, bool]:
        """Idempotently create or retrieve an open Pull Request.

        Returns (pull_request_info, reused_existing).
        """
        existing = self.find_open_pull_request(
            full_name,
            head_branch=head_branch,
            base_branch=base_branch,
            installation_id=installation_id,
        )
        if existing is not None:
            return existing, True

        owner, repo = self._validate_full_name(full_name)
        data = self._post(
            f"/repos/{quote(owner)}/{quote(repo)}/pulls",
            resource=f"{full_name}:create_pull:{head_branch}",
            json_body={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
                "draft": draft,
            },
            installation_id=installation_id,
        )
        return _parse_pr_dict(data), False

    def get_pull_request(
        self,
        full_name: str,
        pr_number: int,
        *,
        installation_id: str | None = None,
    ) -> GitHubPullRequestInfo:
        """Fetch current metadata for an existing Pull Request."""
        owner, repo = self._validate_full_name(full_name)
        data = self._get(
            f"/repos/{quote(owner)}/{quote(repo)}/pulls/{pr_number}",
            resource=f"{full_name}#{pr_number}",
            installation_id=installation_id,
        )
        return _parse_pr_dict(data)

    def get_pull_request_status(
        self,
        full_name: str,
        pr_number: int,
        *,
        installation_id: str | None = None,
    ) -> GitHubPullRequestStatusSnapshot:
        """Fetch PR details, review states, and CI check/commit status on demand."""
        owner, repo = self._validate_full_name(full_name)
        pr_info = self.get_pull_request(
            full_name,
            pr_number,
            installation_id=installation_id,
        )

        raw_reviews = self._get_list(
            f"/repos/{quote(owner)}/{quote(repo)}/pulls/{pr_number}/reviews",
            resource=f"{full_name}#{pr_number}:reviews",
            installation_id=installation_id,
        )
        reviews: list[GitHubReviewSummary] = []
        for item in raw_reviews:
            raw_user = item.get("user")
            user_obj: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
            reviewer = str(user_obj.get("login", "unknown"))
            state_str = str(item.get("state", "PENDING"))
            submitted_at: datetime | None = None
            raw_sub = item.get("submitted_at")
            if isinstance(raw_sub, str):
                try:
                    submitted_at = datetime.fromisoformat(raw_sub.replace("Z", "+00:00"))
                except ValueError:
                    submitted_at = None
            reviews.append(
                GitHubReviewSummary(
                    reviewer=reviewer,
                    state=state_str,
                    submitted_at=submitted_at,
                )
            )

        checks: list[GitHubCheckRunSummary] = []
        combined_state: str | None = None
        if pr_info.head_sha:
            try:
                check_data = self._get(
                    f"/repos/{quote(owner)}/{quote(repo)}/commits/{quote(pr_info.head_sha)}/check-runs",
                    resource=f"{full_name}@{pr_info.head_sha}:check-runs",
                    installation_id=installation_id,
                )
                raw_runs = check_data.get("check_runs", [])
                if isinstance(raw_runs, list):
                    for run in raw_runs:
                        if not isinstance(run, dict):
                            continue
                        checks.append(
                            GitHubCheckRunSummary(
                                name=str(run.get("name", "check")),
                                status=str(run.get("status", "unknown")),
                                conclusion=(
                                    str(run["conclusion"])
                                    if run.get("conclusion") is not None
                                    else None
                                ),
                                details_url=(
                                    str(run["details_url"])
                                    if run.get("details_url") is not None
                                    else None
                                ),
                            )
                        )
            except AppError as exc:
                logger.warning(
                    "Unable to retrieve GitHub check runs",
                    extra={"structured_data": {"repo": full_name, "code": exc.code}},
                )

            try:
                status_data = self._get(
                    f"/repos/{quote(owner)}/{quote(repo)}/commits/{quote(pr_info.head_sha)}/status",
                    resource=f"{full_name}@{pr_info.head_sha}:status",
                    installation_id=installation_id,
                )
                raw_state = status_data.get("state")
                if isinstance(raw_state, str):
                    combined_state = raw_state
            except AppError as exc:
                logger.warning(
                    "Unable to retrieve GitHub combined commit status",
                    extra={"structured_data": {"repo": full_name, "code": exc.code}},
                )

        return GitHubPullRequestStatusSnapshot(
            pull_request=pr_info,
            ci_status=_derive_ci_status(checks, combined_state),
            review_decision=_derive_review_decision(reviews),
            checks=checks,
            reviews=reviews,
        )

    def build_authenticated_clone_url(
        self,
        clone_url: str,
        *,
        installation_id: str | None = None,
    ) -> str:
        """Inject a short-lived token into an HTTPS clone URL for isolated Git operations.

        Callers must never log or persist the returned URL.
        """
        parsed = urlsplit(clone_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValidationFailedError(
                "Only HTTPS clone URLs are permitted for authenticated remote cloning."
            )
        access_token = self._get_installation_token(installation_id=installation_id)
        host_port = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        netloc = f"x-access-token:{quote(access_token, safe='')}@{host_port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))