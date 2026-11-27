"""Isolated local Git workspace manager with base-SHA binding and stale-change protection."""

import logging
import os
import re
import shutil
import stat
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, Repo
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, ConflictError, ValidationFailedError
from app.core.logging import redact_text

logger = logging.getLogger(__name__)

_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{1,126}[A-Za-z0-9]$")
_FORBIDDEN_TARGET_BRANCHES: frozenset[str] = frozenset({"main", "master", "head"})
_LFS_POINTER_HEADER = "version https://git-lfs.github.com/spec/v1"


class WorkspaceInfo(BaseModel):
    """Validated metadata describing a prepared, isolated repository workspace."""

    workspace_id: str
    workspace_path: str
    base_commit_sha: str
    size_bytes: int = Field(ge=0)
    has_submodules: bool = False
    has_git_lfs: bool = False
    warnings: list[str] = Field(default_factory=list)


class GitCommitPushResult(BaseModel):
    """Auditable outcome of a controlled branch creation, commit, and non-force push."""

    branch_name: str
    base_commit_sha: str
    commit_sha: str
    pushed: bool
    reused_existing_commit: bool = False


class WorkspaceService:
    """Manages isolated Git workspaces under a confined root with quota and SHA checks."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._root = self._settings.WORKSPACE_ROOT_DIR.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root_dir(self) -> Path:
        """Return the resolved root directory containing managed workspaces."""
        return self._root

    def _resolve_workspace_path(self, workspace_id: str) -> Path:
        if not _WORKSPACE_ID_PATTERN.match(workspace_id):
            raise ValidationFailedError(
                "Workspace ID contains invalid characters.",
                details={"workspace_id": workspace_id},
            )
        candidate = (self._root / workspace_id).resolve()
        if not candidate.is_relative_to(self._root) or candidate == self._root:
            raise ValidationFailedError(
                "Resolved workspace path escapes configured workspace root.",
                details={"workspace_id": workspace_id},
            )
        return candidate

    def _validate_feature_branch_name(self, branch_name: str, *, base_branch: str) -> str:
        cleaned = branch_name.strip()
        if (
            not _SAFE_BRANCH_PATTERN.match(cleaned)
            or ".." in cleaned
            or "//" in cleaned
            or cleaned.lower() in _FORBIDDEN_TARGET_BRANCHES
            or cleaned == base_branch.strip()
        ):
            raise ValidationFailedError(
                "Invalid or forbidden target feature branch name.",
                details={"branch_name": branch_name, "base_branch": base_branch},
            )
        return cleaned

    def _directory_size_bytes(self, directory: Path) -> int:
        total = 0
        for root, _dirs, files in os.walk(directory):
            for filename in files:
                file_path = Path(root) / filename
                try:
                    if not file_path.is_symlink():
                        total += file_path.stat().st_size
                except OSError:
                    continue
        return total

    def _detect_unsupported_git_features(
        self,
        workspace_path: Path,
    ) -> tuple[bool, bool, list[str]]:
        """Detect Git submodules and Git LFS configurations unsupported in MVP."""
        warnings: list[str] = []
        has_submodules = (workspace_path / ".gitmodules").is_file()
        if has_submodules:
            warnings.append(
                "Git submodules detected (.gitmodules); submodules are unsupported and not fetched."
            )

        has_git_lfs = False
        gitattributes_path = workspace_path / ".gitattributes"
        if gitattributes_path.is_file():
            try:
                attrs_text = gitattributes_path.read_text(encoding="utf-8", errors="ignore")
                if "filter=lfs" in attrs_text:
                    has_git_lfs = True
            except OSError:
                pass

        if not has_git_lfs:
            for root, dirs, files in os.walk(workspace_path):
                dirs[:] = [d for d in dirs if d != ".git"]
                for filename in files:
                    file_path = Path(root) / filename
                    try:
                        if file_path.is_symlink() or file_path.stat().st_size > 1024:
                            continue
                        first_line = file_path.read_text(
                            encoding="utf-8",
                            errors="ignore",
                        ).splitlines()[:1]
                        if first_line and first_line[0].strip() == _LFS_POINTER_HEADER:
                            has_git_lfs = True
                            break
                    except OSError:
                        continue
                if has_git_lfs:
                    break

        if has_git_lfs:
            warnings.append(
                "Git LFS configuration or pointer files detected; Git LFS is unsupported."
            )

        return has_submodules, has_git_lfs, warnings

    def prepare_workspace(
        self,
        *,
        workspace_id: str,
        clone_source: str,
        base_commit_sha: str,
        branch_name: str | None = None,
        allow_unsupported_features: bool = True,
    ) -> WorkspaceInfo:
        """Clone repository into an isolated workspace and pin HEAD to base_commit_sha."""
        workspace_path = self._resolve_workspace_path(workspace_id)
        if workspace_path.exists():
            self.cleanup_workspace(workspace_id)

        repo: Repo | None = None
        try:
            if branch_name:
                repo = Repo.clone_from(
                    clone_source,
                    str(workspace_path),
                    no_checkout=True,
                    branch=branch_name,
                )
            else:
                repo = Repo.clone_from(
                    clone_source,
                    str(workspace_path),
                    no_checkout=True,
                )
            repo.git.checkout("--detach", base_commit_sha)
            resolved_sha = str(repo.head.commit.hexsha)
            if resolved_sha != base_commit_sha:
                raise ConflictError(
                    "Workspace HEAD SHA does not match expected base commit SHA.",
                    details={
                        "expected_sha": base_commit_sha,
                        "actual_sha": resolved_sha,
                    },
                )

            size_bytes = self._directory_size_bytes(workspace_path)
            max_repo_bytes = self._settings.MAX_REPO_SIZE_MB * 1024 * 1024
            max_quota_bytes = self._settings.WORKSPACE_DISK_QUOTA_MB * 1024 * 1024
            if size_bytes > max_repo_bytes or size_bytes > max_quota_bytes:
                raise ValidationFailedError(
                    "Repository workspace exceeds configured disk size limit.",
                    details={
                        "size_bytes": size_bytes,
                        "max_repo_bytes": max_repo_bytes,
                        "max_quota_bytes": max_quota_bytes,
                    },
                )

            has_submodules, has_git_lfs, warnings = self._detect_unsupported_git_features(
                workspace_path
            )
            if not allow_unsupported_features and (has_submodules or has_git_lfs):
                raise ValidationFailedError(
                    "Repository uses unsupported Git features (submodules or Git LFS).",
                    details={
                        "has_submodules": has_submodules,
                        "has_git_lfs": has_git_lfs,
                        "warnings": warnings,
                    },
                )

            return WorkspaceInfo(
                workspace_id=workspace_id,
                workspace_path=str(workspace_path),
                base_commit_sha=resolved_sha,
                size_bytes=size_bytes,
                has_submodules=has_submodules,
                has_git_lfs=has_git_lfs,
                warnings=warnings,
            )
        except (ConflictError, ValidationFailedError):
            if repo is not None:
                repo.close()
            self.cleanup_workspace(workspace_id)
            raise
        except GitCommandError as exc:
            if repo is not None:
                repo.close()
            self.cleanup_workspace(workspace_id)
            safe_stderr = redact_text(str(exc.stderr or exc))
            logger.error(
                "Git command failed during workspace preparation",
                extra={"structured_data": {"workspace_id": workspace_id, "error": safe_stderr}},
            )
            raise AppError(
                "Failed to prepare isolated Git workspace.",
                code="GIT_WORKSPACE_ERROR",
                status_code=500,
                details={"workspace_id": workspace_id},
            ) from exc
        finally:
            if repo is not None:
                repo.close()

    def verify_base_sha(self, *, workspace_id: str, expected_base_sha: str) -> str:
        """Verify workspace HEAD matches expected_base_sha."""
        workspace_path = self._resolve_workspace_path(workspace_id)
        try:
            repo = Repo(str(workspace_path))
        except InvalidGitRepositoryError as exc:
            raise ValidationFailedError(
                "Workspace does not contain a valid Git repository.",
                details={"workspace_id": workspace_id},
            ) from exc

        try:
            current_sha = str(repo.head.commit.hexsha)
            if current_sha != expected_base_sha:
                raise ConflictError(
                    "Workspace base commit SHA drifted from expected version.",
                    details={
                        "expected_base_sha": expected_base_sha,
                        "actual_base_sha": current_sha,
                    },
                )
            return current_sha
        finally:
            repo.close()

    def detect_stale_base(
        self,
        *,
        expected_base_sha: str,
        current_remote_sha: str,
        branch_name: str,
    ) -> None:
        """Raise ConflictError if the remote target branch advanced past base_commit_sha."""
        if expected_base_sha != current_remote_sha:
            raise ConflictError(
                "Target branch has advanced since workflow started; rebase or re-analysis needed.",
                details={
                    "branch_name": branch_name,
                    "expected_base_sha": expected_base_sha,
                    "current_remote_sha": current_remote_sha,
                },
            )

    def _ls_remote_branch_sha(self, repo: Repo, remote_url: str, branch_name: str) -> str | None:
        raw_out = str(repo.git.ls_remote(remote_url, f"refs/heads/{branch_name}")).strip()
        if not raw_out:
            return None
        first_line = raw_out.splitlines()[0].strip()
        parts = first_line.split()
        return parts[0] if parts else None

    def create_branch_commit_and_push(
        self,
        *,
        workspace_id: str,
        branch_name: str,
        base_branch: str,
        expected_base_sha: str,
        commit_message: str,
        remote_push_url: str | None = None,
        author_name: str = "AI SDLC Automation",
        author_email: str = "ai-sdlc-bot@users.noreply.local",
    ) -> GitCommitPushResult:
        """Idempotently create feature branch, commit validated changes, and push non-force.

        Remote push credentials are passed ephemerally to git commands and never written
        to .git/config. Force-push is strictly forbidden.
        """
        safe_branch = self._validate_feature_branch_name(branch_name, base_branch=base_branch)
        workspace_path = self._resolve_workspace_path(workspace_id)
        try:
            repo = Repo(str(workspace_path))
        except InvalidGitRepositoryError as exc:
            raise ValidationFailedError(
                "Workspace does not contain a valid Git repository.",
                details={"workspace_id": workspace_id},
            ) from exc

        try:
            current_sha = str(repo.head.commit.hexsha)
            is_detached = repo.head.is_detached
            active_branch = None if is_detached else str(repo.active_branch.name)

            reused_existing_commit = False
            commit_sha: str

            if active_branch == safe_branch and not repo.is_dirty(untracked_files=True):
                parent_shas = [str(p.hexsha) for p in repo.head.commit.parents]
                if expected_base_sha not in parent_shas:
                    raise ConflictError(
                        "Existing local feature branch is not parented at expected base SHA.",
                        details={
                            "branch_name": safe_branch,
                            "expected_base_sha": expected_base_sha,
                            "parent_shas": parent_shas,
                        },
                    )
                commit_sha = current_sha
                reused_existing_commit = True
            else:
                if current_sha != expected_base_sha:
                    raise ConflictError(
                        "Workspace HEAD drifted from expected base_commit_sha before commit.",
                        details={
                            "expected_base_sha": expected_base_sha,
                            "actual_sha": current_sha,
                        },
                    )

                if not repo.is_dirty(untracked_files=True):
                    raise ValidationFailedError(
                        "Workspace has no modified or untracked files to commit.",
                        details={"workspace_id": workspace_id},
                    )

                if safe_branch in [h.name for h in repo.heads]:
                    repo.git.checkout(safe_branch)
                else:
                    repo.git.checkout("-b", safe_branch)

                with repo.config_writer() as cfg:
                    cfg.set_value("user", "name", author_name)
                    cfg.set_value("user", "email", author_email)

                repo.git.add(A=True)
                commit_obj = repo.index.commit(commit_message)
                commit_sha = str(commit_obj.hexsha)

            pushed = False
            if remote_push_url:
                remote_base_sha = self._ls_remote_branch_sha(repo, remote_push_url, base_branch)
                if remote_base_sha is not None:
                    self.detect_stale_base(
                        expected_base_sha=expected_base_sha,
                        current_remote_sha=remote_base_sha,
                        branch_name=base_branch,
                    )

                remote_head_sha = self._ls_remote_branch_sha(repo, remote_push_url, safe_branch)
                if remote_head_sha is not None:
                    if remote_head_sha == commit_sha:
                        return GitCommitPushResult(
                            branch_name=safe_branch,
                            base_commit_sha=expected_base_sha,
                            commit_sha=commit_sha,
                            pushed=True,
                            reused_existing_commit=reused_existing_commit,
                        )
                    raise ConflictError(
                        f"Remote branch '{safe_branch}' already exists with commit "
                        f"'{remote_head_sha}'; force-push is forbidden without human approval.",
                        details={
                            "branch_name": safe_branch,
                            "remote_head_sha": remote_head_sha,
                            "local_commit_sha": commit_sha,
                        },
                    )

                # Non-force push passing ephemeral URL directly so no token is saved in .git/config
                repo.git.push(remote_push_url, f"HEAD:refs/heads/{safe_branch}")
                pushed = True

            return GitCommitPushResult(
                branch_name=safe_branch,
                base_commit_sha=expected_base_sha,
                commit_sha=commit_sha,
                pushed=pushed,
                reused_existing_commit=reused_existing_commit,
            )
        except (ConflictError, ValidationFailedError):
            raise
        except GitCommandError as exc:
            safe_stderr = redact_text(str(exc.stderr or exc))
            logger.error(
                "Git commit/push operation failed",
                extra={
                    "structured_data": {
                        "workspace_id": workspace_id,
                        "branch_name": safe_branch,
                        "error": safe_stderr,
                    }
                },
            )
            if "non-fast-forward" in safe_stderr.lower() or "rejected" in safe_stderr.lower():
                raise ConflictError(
                    "Remote rejected non-fast-forward branch update; force-push is forbidden.",
                    details={"branch_name": safe_branch},
                ) from exc
            raise AppError(
                "Failed to commit or push changes from isolated workspace.",
                code="GIT_DELIVERY_ERROR",
                status_code=502,
                details={"workspace_id": workspace_id, "branch_name": safe_branch},
            ) from exc
        finally:
            repo.close()

    def cleanup_workspace(self, workspace_id: str) -> bool:
        """Remove an isolated workspace directory safely on all platforms including Windows."""
        workspace_path = self._resolve_workspace_path(workspace_id)
        if not workspace_path.exists():
            return False

        for root, dirs, files in os.walk(workspace_path):
            for dir_name in dirs:
                try:
                    os.chmod(Path(root) / dir_name, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                except OSError:
                    continue
            for file_name in files:
                file_path = Path(root) / file_name
                if not file_path.is_symlink():
                    try:
                        os.chmod(file_path, stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        continue

        shutil.rmtree(workspace_path)
        return True