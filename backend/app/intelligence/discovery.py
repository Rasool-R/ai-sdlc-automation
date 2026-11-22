"""Safe repository file discovery with ignore rules, binary filtering, and content hashing."""

import hashlib
import os
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.exceptions import ValidationFailedError
from app.intelligence.schemas import DiscoveredFile

IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "htmlcov",
        ".next",
        "out",
        "site-packages",
    }
)

IGNORED_FILE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".bin",
        ".class",
        ".jar",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp3",
        ".mp4",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".lock",
    }
)

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".ini": "ini",
    ".cfg": "ini",
    ".txt": "text",
    ".sql": "sql",
}

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024


def compute_sha256_bytes(content: bytes) -> str:
    """Return lowercase hexadecimal SHA-256 digest for raw bytes."""
    return hashlib.sha256(content).hexdigest()


def is_binary_bytes(sample: bytes) -> bool:
    """Heuristically identify binary content via null-byte or UTF-8 decode failure."""
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def is_python_test_path(relative_path: str) -> bool:
    """Determine whether a normalized POSIX relative path represents a Python test file."""
    path_obj = Path(relative_path)
    if path_obj.suffix != ".py":
        return False
    name = path_obj.name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    return any(part in {"tests", "test"} for part in path_obj.parts[:-1])


class RepositoryDiscoveryService:
    """Walks an isolated workspace and discovers indexable text/Python files safely."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self._settings = settings or get_settings()
        self._max_file_bytes = max_file_bytes

    def _should_ignore_dir(self, dir_name: str) -> bool:
        return dir_name in IGNORED_DIR_NAMES or dir_name.endswith(".egg-info")

    def _should_ignore_file(self, path: Path) -> bool:
        if path.suffix.lower() in IGNORED_FILE_SUFFIXES:
            return True
        return path.name in {".DS_Store", "Thumbs.db"}

    def discover(self, workspace_dir: Path) -> tuple[list[DiscoveredFile], int]:
        """Discover indexable files under workspace_dir and return (files, skipped_count)."""
        root = workspace_dir.resolve()
        if not root.is_dir():
            raise ValidationFailedError(
                "Workspace directory does not exist.",
                details={"workspace_dir": str(workspace_dir)},
            )

        discovered: list[DiscoveredFile] = []
        skipped_count = 0
        python_file_count = 0
        max_python_files = self._settings.MAX_REPO_PYTHON_FILES

        for current_root, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not self._should_ignore_dir(d))
            for filename in sorted(files):
                file_path = Path(current_root) / filename
                if file_path.is_symlink():
                    skipped_count += 1
                    continue

                resolved_file = file_path.resolve()
                if not resolved_file.is_relative_to(root):
                    skipped_count += 1
                    continue

                if self._should_ignore_file(file_path):
                    skipped_count += 1
                    continue

                try:
                    stat_info = file_path.stat()
                except OSError:
                    skipped_count += 1
                    continue

                if stat_info.st_size > self._max_file_bytes:
                    skipped_count += 1
                    continue

                try:
                    raw_bytes = file_path.read_bytes()
                except OSError:
                    skipped_count += 1
                    continue

                if is_binary_bytes(raw_bytes[:8192]):
                    skipped_count += 1
                    continue

                rel_posix = file_path.relative_to(root).as_posix()
                suffix = file_path.suffix.lower()
                language = LANGUAGE_BY_SUFFIX.get(suffix, "text")
                is_python = suffix == ".py"

                if is_python:
                    python_file_count += 1
                    if python_file_count > max_python_files:
                        raise ValidationFailedError(
                            "Repository exceeds maximum allowed Python file count for analysis.",
                            details={
                                "max_python_files": max_python_files,
                                "discovered_python_files": python_file_count,
                            },
                        )

                discovered.append(
                    DiscoveredFile(
                        relative_path=rel_posix,
                        size_bytes=stat_info.st_size,
                        content_hash=compute_sha256_bytes(raw_bytes),
                        language=language,
                        is_python=is_python,
                        is_test_file=is_python_test_path(rel_posix),
                    )
                )

        return discovered, skipped_count