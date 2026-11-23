"""Backend patch validator and atomic applier enforcing safety invariants and bounds."""

import ast
import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from git import InvalidGitRepositoryError, Repo

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    ValidationFailedError,
)
from app.execution.schemas import (
    FilePatchOperation,
    PatchApplyResult,
    PatchBundle,
    PatchValidationResult,
    ProtectedPathCategory,
    ProtectedPathFlag,
    TestIntegrityFlag,
)
from app.execution.test_integrity import inspect_test_integrity
from app.intelligence.discovery import compute_sha256_bytes, is_binary_bytes

_LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
    }
)
_MANIFEST_NAMES: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "package.json",
    }
)
_SECRET_LIKE_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".p12", ".pfx", ".jks"})


@dataclass
class _PreparedFileWrite:
    relative_path: str
    target_path: Path
    operation: str
    old_text: str | None
    new_text: str | None
    line_ending: str = "\n"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def classify_protected_path(relative_path: str) -> tuple[ProtectedPathCategory, str] | None:
    """Classify whether a normalized POSIX relative path is a protected path."""
    normalized = relative_path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")

    lower = normalized.lower()
    path_obj = Path(normalized)
    name_lower = path_obj.name.lower()

    if (
        lower.startswith(".github/workflows/")
        or lower.startswith(".circleci/")
        or name_lower in {".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml"}
    ):
        return ("ci_workflow", "CI/CD workflow configuration requires explicit human approval.")

    if (
        name_lower == "dockerfile"
        or name_lower.startswith("dockerfile.")
        or name_lower.endswith(".dockerfile")
        or (name_lower.startswith("docker-compose") and path_obj.suffix.lower() in {".yml", ".yaml"})
    ):
        return ("docker", "Container build/runtime configuration requires explicit human approval.")

    if (
        name_lower == ".env"
        or name_lower.startswith(".env.")
        or path_obj.suffix.lower() in _SECRET_LIKE_SUFFIXES
        or "secret" in name_lower
        or name_lower in {"credentials.json", ".npmrc", ".pypirc"}
    ):
        return (
            "secret_or_env",
            "Environment or secret-like file requires explicit human approval.",
        )

    if name_lower in _LOCKFILE_NAMES or (
        name_lower.startswith("requirements") and name_lower.endswith(".lock")
    ):
        return ("lockfile", "Dependency lockfile modification requires explicit human approval.")

    if name_lower in _MANIFEST_NAMES or (
        name_lower.startswith("requirements") and name_lower.endswith(".txt")
    ):
        return (
            "dependency_manifest",
            "Dependency manifest requires plan declaration of dependency change and approval.",
        )

    return None


def _normalize_relative_path(raw_path: str) -> str:
    cleaned = raw_path.replace("\\", "/").strip()
    if not cleaned or "\x00" in cleaned:
        raise ValidationFailedError("Patch path must be a non-empty relative path.")
    if cleaned.startswith("/") or ":" in cleaned.split("/", 1)[0]:
        raise ValidationFailedError(
            "Absolute or drive-qualified patch paths are forbidden.",
            details={"path": raw_path},
        )

    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValidationFailedError(
            "Path traversal segments ('..') are forbidden in patches.",
            details={"path": raw_path},
        )
    if parts[0] == ".git":
        raise ValidationFailedError(
            "Direct modification of '.git' internals is forbidden.",
            details={"path": raw_path},
        )
    return "/".join(parts)


def _ensure_no_symlink_escape(workspace_root: Path, rel_path: str) -> Path:
    current = workspace_root
    for part in Path(rel_path).parts:
        current = current / part
        if current.is_symlink():
            raise ValidationFailedError(
                "Patch target traverses or targets a symbolic link, which is forbidden.",
                details={"path": rel_path},
            )
    resolved = current.resolve()
    if not resolved.is_relative_to(workspace_root) or resolved == workspace_root:
        raise ValidationFailedError(
            "Resolved patch path escapes workspace boundary.",
            details={"path": rel_path},
        )
    return resolved


def _apply_hunks(
    *,
    original_text: str,
    op: FilePatchOperation,
    rel_path: str,
) -> str:
    updated = original_text
    for idx, hunk in enumerate(op.hunks):
        search_norm = _normalize_newlines(hunk.search)
        replace_norm = _normalize_newlines(hunk.replace)
        count = updated.count(search_norm)
        if count != hunk.expected_occurrences:
            raise ConflictError(
                f"Patch hunk #{idx + 1} for '{rel_path}' matched {count} occurrence(s), "
                f"expected {hunk.expected_occurrences}.",
                details={
                    "path": rel_path,
                    "hunk_index": idx,
                    "expected_occurrences": hunk.expected_occurrences,
                    "actual_occurrences": count,
                },
            )
        updated = updated.replace(search_norm, replace_norm, hunk.expected_occurrences)
    return updated


def _build_unified_diff(writes: list[_PreparedFileWrite]) -> tuple[str, int, int]:
    diff_chunks: list[str] = []
    lines_added = 0
    lines_removed = 0

    for item in writes:
        old_lines = (
            item.old_text.splitlines(keepends=True) if item.old_text is not None else []
        )
        new_lines = (
            item.new_text.splitlines(keepends=True) if item.new_text is not None else []
        )
        fromfile = "/dev/null" if item.operation == "create" else f"a/{item.relative_path}"
        tofile = "/dev/null" if item.operation == "delete" else f"b/{item.relative_path}"

        file_diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=fromfile,
                tofile=tofile,
                lineterm="\n",
            )
        )
        for line in file_diff:
            if line.startswith("+") and not line.startswith("+++"):
                lines_added += 1
            elif line.startswith("-") and not line.startswith("---"):
                lines_removed += 1

        if file_diff:
            chunk = "".join(line if line.endswith("\n") else f"{line}\n" for line in file_diff)
            diff_chunks.append(chunk)

    return "".join(diff_chunks), lines_added, lines_removed


class PatchService:
    """Validates and atomically applies bounded structured patches to isolated workspaces."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _verify_git_head(self, workspace_root: Path, expected_base_sha: str) -> None:
        try:
            repo = Repo(str(workspace_root))
        except InvalidGitRepositoryError as exc:
            raise ValidationFailedError(
                "Target workspace is not a valid Git repository.",
                details={"workspace_dir": str(workspace_root)},
            ) from exc

        try:
            actual_sha = str(repo.head.commit.hexsha)
            if actual_sha != expected_base_sha:
                raise ConflictError(
                    "Workspace base commit SHA does not match patch base_commit_sha.",
                    details={
                        "expected_base_sha": expected_base_sha,
                        "actual_base_sha": actual_sha,
                    },
                )
        finally:
            repo.close()

    def _prepare_and_validate(
        self,
        *,
        workspace_dir: Path,
        patch: PatchBundle,
        expected_files: list[str] | None = None,
        declared_protected_paths: list[str] | None = None,
        approved_protected_paths: list[str] | None = None,
        plan_declares_dependency_change: bool = False,
        verify_git_head: bool = True,
    ) -> tuple[PatchValidationResult, list[_PreparedFileWrite]]:
        workspace_root = workspace_dir.resolve()
        if not workspace_root.is_dir():
            raise ValidationFailedError(
                "Workspace directory does not exist.",
                details={"workspace_dir": str(workspace_dir)},
            )

        if verify_git_head:
            self._verify_git_head(workspace_root, patch.base_commit_sha)

        if len(patch.operations) > self._settings.MAX_FILES_PER_PATCH:
            raise ValidationFailedError(
                f"Patch touches {len(patch.operations)} files, exceeding limit of "
                f"{self._settings.MAX_FILES_PER_PATCH}.",
                details={
                    "files_count": len(patch.operations),
                    "max_files_per_patch": self._settings.MAX_FILES_PER_PATCH,
                },
            )

        expected_set = (
            {_normalize_relative_path(p) for p in expected_files}
            if expected_files is not None
            else None
        )
        declared_protected_set = {
            _normalize_relative_path(p) for p in (declared_protected_paths or [])
        }
        approved_protected_set = {
            _normalize_relative_path(p) for p in (approved_protected_paths or [])
        }

        seen_paths: set[str] = set()
        prepared_writes: list[_PreparedFileWrite] = []
        protected_flags: list[ProtectedPathFlag] = []
        scope_drift_files: list[str] = []
        test_integrity_flags: list[TestIntegrityFlag] = []
        errors: list[str] = []

        for op in patch.operations:
            rel_path = _normalize_relative_path(op.path)
            if rel_path in seen_paths:
                raise ValidationFailedError(
                    f"Duplicate patch operation for path '{rel_path}'.",
                    details={"path": rel_path},
                )
            seen_paths.add(rel_path)
            target_path = _ensure_no_symlink_escape(workspace_root, rel_path)

            protected_match = classify_protected_path(rel_path)
            if protected_match is not None:
                category, reason = protected_match
                is_declared = rel_path in declared_protected_set or (
                    category == "dependency_manifest" and plan_declares_dependency_change
                )
                is_approved = rel_path in approved_protected_set
                protected_flags.append(
                    ProtectedPathFlag(
                        file_path=rel_path,
                        category=category,
                        reason=reason,
                        declared_in_plan=is_declared,
                        approved=is_approved,
                    )
                )

            if expected_set is not None and rel_path not in expected_set:
                scope_drift_files.append(rel_path)

            old_text: str | None = None
            new_text: str | None = None
            line_ending = "\n"

            if op.operation == "create":
                if target_path.exists():
                    raise ConflictError(
                        f"Cannot create '{rel_path}' because it already exists.",
                        details={"path": rel_path},
                    )
                raw_new = (op.content or "").encode("utf-8")
                if is_binary_bytes(raw_new[:8192]):
                    raise ValidationFailedError(
                        f"Binary content is forbidden in patch for '{rel_path}'.",
                        details={"path": rel_path},
                    )
                new_text = _normalize_newlines(op.content or "")
            else:
                if not target_path.is_file():
                    raise ConflictError(
                        f"Target file '{rel_path}' does not exist in workspace.",
                        details={"path": rel_path},
                    )
                raw_existing = target_path.read_bytes()
                if is_binary_bytes(raw_existing[:8192]):
                    raise ValidationFailedError(
                        f"Binary file '{rel_path}' cannot be patched.",
                        details={"path": rel_path},
                    )
                actual_hash = compute_sha256_bytes(raw_existing)
                if actual_hash != op.base_content_hash:
                    raise ConflictError(
                        f"Base content hash mismatch for '{rel_path}'; file changed since read.",
                        details={
                            "path": rel_path,
                            "expected_hash": op.base_content_hash,
                            "actual_hash": actual_hash,
                        },
                    )
                decoded_existing = raw_existing.decode("utf-8")
                if "\r\n" in decoded_existing:
                    line_ending = "\r\n"
                old_text = _normalize_newlines(decoded_existing)
                if op.operation == "modify":
                    new_text = _apply_hunks(
                        original_text=old_text,
                        op=op,
                        rel_path=rel_path,
                    )
                    if is_binary_bytes(new_text.encode("utf-8")[:8192]):
                        raise ValidationFailedError(
                            f"Resulting content for '{rel_path}' is binary.",
                            details={"path": rel_path},
                        )

            if rel_path.endswith(".py") and new_text is not None:
                try:
                    ast.parse(new_text, filename=rel_path)
                except SyntaxError as exc:
                    raise ValidationFailedError(
                        f"Patched Python file '{rel_path}' contains a syntax error: {exc.msg}",
                        details={"path": rel_path, "line": exc.lineno},
                    ) from exc

            test_flags = inspect_test_integrity(
                file_path=rel_path,
                operation=op.operation,
                old_content=old_text,
                new_content=new_text,
            )
            test_integrity_flags.extend(test_flags)

            prepared_writes.append(
                _PreparedFileWrite(
                    relative_path=rel_path,
                    target_path=target_path,
                    operation=op.operation,
                    old_text=old_text,
                    new_text=new_text,
                    line_ending=line_ending,
                )
            )

        unified_diff, lines_added, lines_removed = _build_unified_diff(prepared_writes)
        total_changed_lines = lines_added + lines_removed
        if total_changed_lines > self._settings.MAX_LINES_PER_PATCH:
            raise ValidationFailedError(
                f"Patch changes {total_changed_lines} lines, exceeding limit of "
                f"{self._settings.MAX_LINES_PER_PATCH}.",
                details={
                    "total_changed_lines": total_changed_lines,
                    "max_lines_per_patch": self._settings.MAX_LINES_PER_PATCH,
                },
            )

        unapproved_protected = [
            flag for flag in protected_flags if not (flag.declared_in_plan and flag.approved)
        ]
        if unapproved_protected:
            errors.append(
                "Patch modifies protected path(s) without plan declaration and human approval."
            )
        if test_integrity_flags:
            errors.append(
                "Patch triggers test-integrity warning(s) requiring explicit human approval."
            )

        patch_hash = hashlib.sha256(
            f"{patch.base_commit_sha}\n{unified_diff}".encode()
        ).hexdigest()
        requires_approval = bool(unapproved_protected or test_integrity_flags)

        result = PatchValidationResult(
            valid=len(errors) == 0,
            base_commit_sha=patch.base_commit_sha,
            patch_hash=patch_hash,
            unified_diff=unified_diff,
            files_changed=[w.relative_path for w in prepared_writes],
            lines_added=lines_added,
            lines_removed=lines_removed,
            total_changed_lines=total_changed_lines,
            protected_path_flags=protected_flags,
            scope_drift_files=scope_drift_files,
            test_integrity_flags=test_integrity_flags,
            requires_human_approval=requires_approval,
            errors=errors,
        )
        return result, prepared_writes

    def validate_patch(
        self,
        *,
        workspace_dir: Path,
        patch: PatchBundle,
        expected_files: list[str] | None = None,
        declared_protected_paths: list[str] | None = None,
        approved_protected_paths: list[str] | None = None,
        plan_declares_dependency_change: bool = False,
        verify_git_head: bool = True,
    ) -> PatchValidationResult:
        """Validate a PatchBundle in memory without mutating the workspace."""
        result, _writes = self._prepare_and_validate(
            workspace_dir=workspace_dir,
            patch=patch,
            expected_files=expected_files,
            declared_protected_paths=declared_protected_paths,
            approved_protected_paths=approved_protected_paths,
            plan_declares_dependency_change=plan_declares_dependency_change,
            verify_git_head=verify_git_head,
        )
        return result

    def apply_patch(
        self,
        *,
        workspace_dir: Path,
        patch: PatchBundle,
        expected_files: list[str] | None = None,
        declared_protected_paths: list[str] | None = None,
        approved_protected_paths: list[str] | None = None,
        plan_declares_dependency_change: bool = False,
        allow_test_integrity_override: bool = False,
        verify_git_head: bool = True,
    ) -> PatchApplyResult:
        """Validate all operations first, enforce approval gates, then apply atomically."""
        validation, prepared_writes = self._prepare_and_validate(
            workspace_dir=workspace_dir,
            patch=patch,
            expected_files=expected_files,
            declared_protected_paths=declared_protected_paths,
            approved_protected_paths=approved_protected_paths,
            plan_declares_dependency_change=plan_declares_dependency_change,
            verify_git_head=verify_git_head,
        )

        unapproved_protected = [
            f for f in validation.protected_path_flags if not (f.declared_in_plan and f.approved)
        ]
        if unapproved_protected:
            raise AuthorizationError(
                "Protected path modification denied without plan declaration and human approval.",
                details={
                    "protected_paths": [f.file_path for f in unapproved_protected],
                    "patch_hash": validation.patch_hash,
                },
            )

        if validation.test_integrity_flags and not allow_test_integrity_override:
            raise AuthorizationError(
                "Test-integrity check blocked patch; human approval is required to weaken tests.",
                details={
                    "flags": [f.model_dump() for f in validation.test_integrity_flags],
                    "patch_hash": validation.patch_hash,
                },
            )

        for item in prepared_writes:
            if item.operation == "delete":
                item.target_path.unlink()
            else:
                item.target_path.parent.mkdir(parents=True, exist_ok=True)
                text_to_write = item.new_text or ""
                if item.line_ending == "\r\n":
                    text_to_write = text_to_write.replace("\n", "\r\n")
                item.target_path.write_bytes(text_to_write.encode("utf-8"))

        return PatchApplyResult(
            applied=True,
            base_commit_sha=validation.base_commit_sha,
            patch_hash=validation.patch_hash,
            unified_diff=validation.unified_diff,
            files_changed=validation.files_changed,
            lines_added=validation.lines_added,
            lines_removed=validation.lines_removed,
            total_changed_lines=validation.total_changed_lines,
            protected_path_flags=validation.protected_path_flags,
            scope_drift_files=validation.scope_drift_files,
            test_integrity_flags=validation.test_integrity_flags,
        )