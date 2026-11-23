"""Unit and security tests for the demo FastAPI fixture, PatchService, and test integrity."""

import shutil
from pathlib import Path

import pytest
from app.core.config import load_settings
from app.core.exceptions import AuthorizationError, ConflictError, ValidationFailedError
from app.execution.patch_service import PatchService, classify_protected_path
from app.execution.schemas import FilePatchOperation, PatchBundle, SearchReplaceHunk
from app.intelligence.ast_parser import PythonAstParser
from app.intelligence.discovery import RepositoryDiscoveryService, compute_sha256_bytes
from cryptography.fernet import Fernet
from git import Repo

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_FIXTURE_DIR = REPO_ROOT / "fixtures" / "demo_fastapi_repo"


def _init_disposable_demo_repo(tmp_path: Path) -> tuple[Path, str]:
    """Copy the demo FastAPI fixture into tmp_path as a clean Git repository."""
    dest = tmp_path / "demo_repo"
    shutil.copytree(DEMO_FIXTURE_DIR, dest)
    git_repo = Repo.init(str(dest))
    try:
        with git_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Demo Tester")
            cfg.set_value("user", "email", "demo@example.com")
        git_repo.git.add(A=True)
        commit_sha = str(git_repo.index.commit("Initial demo catalog API").hexsha)
        return dest, commit_sha
    finally:
        git_repo.close()


def _file_hash(workspace: Path, rel_path: str) -> str:
    return compute_sha256_bytes((workspace / rel_path).read_bytes())


def _make_patch_service(**overrides: object) -> PatchService:
    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(CREDENTIAL_ENCRYPTION_KEYS=fernet_key, **overrides)
    return PatchService(settings=settings)


def test_demo_fixture_is_valid_and_parses_cleanly_with_ast(tmp_path: Path) -> None:
    assert DEMO_FIXTURE_DIR.is_dir()
    workspace, commit_sha = _init_disposable_demo_repo(tmp_path)

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(CREDENTIAL_ENCRYPTION_KEYS=fernet_key)
    parser = PythonAstParser(discovery_service=RepositoryDiscoveryService(settings=settings))
    metadata = parser.analyze_workspace(
        workspace_dir=workspace,
        repository_id="demo-repo-id",
        commit_sha=commit_sha,
    )

    assert metadata.parse_errors == []
    symbol_qnames = {s.qualified_name for s in metadata.symbols}
    assert "app.main.list_items" in symbol_qnames
    assert "app.store.list_all_items" in symbol_qnames
    assert "tests.test_items.test_list_items_returns_all_seeded_items" in symbol_qnames


def test_patch_service_validates_applies_and_flags_scope_drift(tmp_path: Path) -> None:
    workspace, commit_sha = _init_disposable_demo_repo(tmp_path)
    service = _make_patch_service()

    readme_hash = _file_hash(workspace, "README.md")
    main_hash = _file_hash(workspace, "app/main.py")

    patch = PatchBundle(
        base_commit_sha=commit_sha,
        summary="Add pagination note and limit/offset query params to GET /items",
        operations=[
            FilePatchOperation(
                path="app/main.py",
                operation="modify",
                base_content_hash=main_hash,
                hunks=[
                    SearchReplaceHunk(
                        search="from fastapi import FastAPI, HTTPException, status",
                        replace="from fastapi import FastAPI, HTTPException, Query, status",
                    ),
                    SearchReplaceHunk(
                        search=(
                            "def list_items() -> ItemListResponse:\n"
                            '    """Return all catalog items."""\n'
                            "    items = list_all_items()\n"
                            "    return ItemListResponse(items=items, total=len(items))"
                        ),
                        replace=(
                            "def list_items(\n"
                            "    limit: int = Query(default=20, ge=1, le=100),\n"
                            "    offset: int = Query(default=0, ge=0),\n"
                            ") -> ItemListResponse:\n"
                            '    """Return paginated catalog items."""\n'
                            "    all_items = list_all_items()\n"
                            "    return ItemListResponse(\n"
                            "        items=all_items[offset : offset + limit],\n"
                            "        total=len(all_items),\n"
                            "    )"
                        ),
                    ),
                ],
            ),
            FilePatchOperation(
                path="README.md",
                operation="modify",
                base_content_hash=readme_hash,
                hunks=[
                    SearchReplaceHunk(
                        search="- `GET /items` — Lists all catalog items" \
                        " along with the total count.",
                        replace=(
                            "- `GET /items` — Lists paginated catalog items "
                            "(`limit`, `offset`) along with the total count."
                        ),
                    )
                ],
            ),
        ],
    )

    # Validate without mutating disk; README.md not in expected_files triggers scope drift flag
    validation = service.validate_patch(
        workspace_dir=workspace,
        patch=patch,
        expected_files=["app/main.py"],
    )
    assert validation.valid is True
    assert validation.requires_human_approval is False
    assert validation.scope_drift_files == ["README.md"]
    assert validation.lines_added > 0
    assert validation.lines_removed > 0
    assert "a/app/main.py" in validation.unified_diff
    # Workspace must remain untouched after validate_patch
    assert _file_hash(workspace, "app/main.py") == main_hash

    # Apply atomically and confirm diff hash determinism
    applied = service.apply_patch(
        workspace_dir=workspace,
        patch=patch,
        expected_files=["app/main.py", "README.md"],
    )
    assert applied.applied is True
    assert applied.patch_hash == validation.patch_hash
    assert applied.scope_drift_files == []
    assert "offset : offset + limit" in (workspace / "app/main.py").read_text(encoding="utf-8")


def test_patch_service_rejects_stale_sha_stale_hash_traversal_and_limits(
    tmp_path: Path,
) -> None:
    workspace, commit_sha = _init_disposable_demo_repo(tmp_path)
    service = _make_patch_service(MAX_FILES_PER_PATCH=2, MAX_LINES_PER_PATCH=10)

    # 1. Stale base commit SHA rejected
    stale_sha_patch = PatchBundle(
        base_commit_sha="f" * 40,
        summary="Stale base SHA",
        operations=[
            FilePatchOperation(
                path="notes.txt",
                operation="create",
                content="hello\n",
            )
        ],
    )
    with pytest.raises(ConflictError):
        service.apply_patch(workspace_dir=workspace, patch=stale_sha_patch)

    # 2. Stale base content hash rejected AND earlier create op is NOT written (atomic)
    stale_hash_patch = PatchBundle(
        base_commit_sha=commit_sha,
        summary="Atomic check with stale hash on second op",
        operations=[
            FilePatchOperation(
                path="app/new_helper.py",
                operation="create",
                content="VALUE = 1\n",
            ),
            FilePatchOperation(
                path="README.md",
                operation="modify",
                base_content_hash="0" * 64,
                hunks=[SearchReplaceHunk(search="Demo", replace="Updated")],
            ),
        ],
    )
    with pytest.raises(ConflictError):
        service.apply_patch(workspace_dir=workspace, patch=stale_hash_patch)
    assert not (workspace / "app/new_helper.py").exists()

    # 3. Path traversal and .git modification rejected
    for forbidden_path in ("../escape.py", ".git/config", "/etc/passwd"):
        bad_path_patch = PatchBundle(
            base_commit_sha=commit_sha,
            summary="Malicious path",
            operations=[
                FilePatchOperation(
                    path=forbidden_path,
                    operation="create",
                    content="evil = True\n",
                )
            ],
        )
        with pytest.raises(ValidationFailedError):
            service.apply_patch(workspace_dir=workspace, patch=bad_path_patch)

    # 4. File count limit and line count limit rejected
    too_many_files = PatchBundle(
        base_commit_sha=commit_sha,
        summary="Exceed file count",
        operations=[
            FilePatchOperation(path="a.txt", operation="create", content="1\n"),
            FilePatchOperation(path="b.txt", operation="create", content="2\n"),
            FilePatchOperation(path="c.txt", operation="create", content="3\n"),
        ],
    )
    with pytest.raises(ValidationFailedError):
        service.apply_patch(workspace_dir=workspace, patch=too_many_files)

    too_many_lines = PatchBundle(
        base_commit_sha=commit_sha,
        summary="Exceed changed line limit",
        operations=[
            FilePatchOperation(
                path="big.txt",
                operation="create",
                content="\n".join(f"line {i}" for i in range(25)) + "\n",
            )
        ],
    )
    with pytest.raises(ValidationFailedError):
        service.apply_patch(workspace_dir=workspace, patch=too_many_lines)


def test_protected_paths_require_declaration_and_human_approval(tmp_path: Path) -> None:
    workspace, commit_sha = _init_disposable_demo_repo(tmp_path)
    service = _make_patch_service()

    assert classify_protected_path(".github/workflows/ci.yml") is not None
    assert classify_protected_path("Dockerfile") is not None
    assert classify_protected_path(".env.production") is not None
    assert classify_protected_path("pyproject.toml") is not None
    assert classify_protected_path("app/main.py") is None

    ci_patch = PatchBundle(
        base_commit_sha=commit_sha,
        summary="Add GitHub Actions workflow",
        operations=[
            FilePatchOperation(
                path=".github/workflows/ci.yml",
                operation="create",
                content="name: CI\non: [push]\n",
            )
        ],
    )

    # Denied without declaration and approval
    validation = service.validate_patch(workspace_dir=workspace, patch=ci_patch)
    assert validation.valid is False
    assert validation.requires_human_approval is True
    assert validation.protected_path_flags[0].category == "ci_workflow"

    with pytest.raises(AuthorizationError):
        service.apply_patch(workspace_dir=workspace, patch=ci_patch)
    assert not (workspace / ".github/workflows/ci.yml").exists()

    # Succeeds once declared in plan AND explicitly approved by human
    applied = service.apply_patch(
        workspace_dir=workspace,
        patch=ci_patch,
        declared_protected_paths=[".github/workflows/ci.yml"],
        approved_protected_paths=[".github/workflows/ci.yml"],
    )
    assert applied.applied is True
    assert (workspace / ".github/workflows/ci.yml").is_file()


def test_test_integrity_blocks_removed_tests_weakened_assertions_and_skips(
    tmp_path: Path,
) -> None:
    workspace, commit_sha = _init_disposable_demo_repo(tmp_path)
    service = _make_patch_service()
    test_file_hash = _file_hash(workspace, "tests/test_items.py")

    tamper_patch = PatchBundle(
        base_commit_sha=commit_sha,
        summary="Attempt to skip and weaken existing tests",
        operations=[
            FilePatchOperation(
                path="tests/test_items.py",
                operation="modify",
                base_content_hash=test_file_hash,
                hunks=[
                    SearchReplaceHunk(
                        search="from fastapi.testclient import TestClient",
                        replace="import pytest\nfrom fastapi.testclient import TestClient",
                    ),
                    SearchReplaceHunk(
                        search=(
                            "def test_list_items_returns_all_seeded_items() -> None:\n"
                            '    response = client.get("/items")\n'
                            "    assert response.status_code == 200\n"
                            "    body = response.json()\n"
                            '    assert body["total"] == 25\n'
                            '    assert len(body["items"]) == 25\n'
                            '    assert body["items"][0]["id"] == "item-001"'
                        ),
                        replace=(
                            "@pytest.mark.skip(reason='hide failure')\n"
                            "def test_list_items_returns_all_seeded_items() -> None:\n"
                            '    response = client.get("/items")\n'
                            "    assert response.status_code == 200"
                        ),
                    ),
                    SearchReplaceHunk(
                        search=(
                            "def test_get_single_item_and_not_found() -> None:\n"
                            '    found = client.get("/items/item-005")\n'
                            "    assert found.status_code == 200\n"
                            '    assert found.json()["name"] == "Catalog Item 005"\n'
                            "\n"
                            '    missing = client.get("/items/item-999")\n'
                            "    assert missing.status_code == 404"
                        ),
                        replace="",
                    ),
                ],
            )
        ],
    )

    validation = service.validate_patch(workspace_dir=workspace, patch=tamper_patch)
    assert validation.valid is False
    assert validation.requires_human_approval is True
    triggered_rules = {f.rule_id for f in validation.test_integrity_flags}
    assert {"test_removed", "assertions_reduced", "skip_marker_added"}.issubset(triggered_rules)

    with pytest.raises(AuthorizationError):
        service.apply_patch(workspace_dir=workspace, patch=tamper_patch)

    # Original test file remains completely unchanged
    assert _file_hash(workspace, "tests/test_items.py") == test_file_hash

    # Explicit human override allows applying when justified
    applied = service.apply_patch(
        workspace_dir=workspace,
        patch=tamper_patch,
        allow_test_integrity_override=True,
    )
    assert applied.applied is True