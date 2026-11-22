"""Unit tests for controlled GitHub integration client and isolated Git workspace service."""

import base64
from pathlib import Path

import httpx
import pytest
from app.core.config import load_settings
from app.core.exceptions import (
    ConflictError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from app.integrations.github.client import GitHubClient
from app.services.workspace_service import WorkspaceService
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from git import Repo


def _create_local_git_repo(
    repo_dir: Path,
    *,
    with_submodules: bool = False,
    with_lfs: bool = False,
) -> tuple[str, str]:
    """Create a disposable local Git repository and return (first_sha, second_sha)."""
    repo = Repo.init(str(repo_dir))
    try:
        with repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Test Developer")
            cfg.set_value("user", "email", "dev@example.com")

        (repo_dir / "app.py").write_text("def greet() -> str:\n    return 'hello'\n", "utf-8")
        if with_submodules:
            (repo_dir / ".gitmodules").write_text('[submodule "sub"]\n\tpath = sub\n', "utf-8")
        if with_lfs:
            (repo_dir / ".gitattributes").write_text("*.bin filter=lfs diff=lfs\n", "utf-8")

        repo.git.add(A=True)
        first_commit = repo.index.commit("Initial commit")

        (repo_dir / "app.py").write_text("def greet() -> str:\n    return 'hello v2'\n", "utf-8")
        repo.git.add(A=True)
        second_commit = repo.index.commit("Update greeting")
        return str(first_commit.hexsha), str(second_commit.hexsha)
    finally:
        repo.close()


def test_github_client_pat_fallback_inspection_and_no_merge_paths() -> None:
    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        GITHUB_AUTH_MODE="pat_fallback",
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testtoken1234567890abcdefghij",  # noqa: S106
    )

    encoded_py = base64.b64encode(b"print('ok')\n").decode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer ghp_testtoken1234567890abcdefghij"
        path = request.url.path
        if path == "/repos/acme/service":
            return httpx.Response(
                200,
                json={
                    "full_name": "acme/service",
                    "name": "service",
                    "default_branch": "main",
                    "clone_url": "https://github.com/acme/service.git",
                    "private": True,
                },
            )
        if path == "/repos/acme/service/branches/main":
            return httpx.Response(
                200,
                json={"name": "main", "commit": {"sha": "a" * 40}, "protected": True},
            )
        if path == f"/repos/acme/service/commits/{'a' * 40}":
            return httpx.Response(
                200,
                json={
                    "sha": "a" * 40,
                    "commit": {
                        "message": "Initial commit",
                        "author": {"name": "Alice", "date": "2026-11-20T10:00:00Z"},
                    },
                },
            )
        if path == f"/repos/acme/service/git/trees/{'a' * 40}":
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "app/main.py", "type": "blob", "sha": "b" * 40, "size": 42},
                    ]
                },
            )
        if path == "/repos/acme/service/contents/app/main.py":
            return httpx.Response(
                200,
                json={"encoding": "base64", "content": encoded_py},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as mock_http:
        client = GitHubClient(settings=settings, http_client=mock_http)

        repo_info = client.get_repository("acme/service")
        assert repo_info.full_name == "acme/service"
        assert repo_info.is_private is True

        branch_info = client.get_branch("acme/service", "main")
        assert branch_info.commit_sha == "a" * 40
        assert branch_info.protected is True

        commit_info = client.get_commit("acme/service", "a" * 40)
        assert commit_info.author_name == "Alice"
        assert commit_info.committed_at is not None

        tree = client.list_tree_files("acme/service", "a" * 40)
        assert len(tree) == 1
        assert tree[0].path == "app/main.py"

        content = client.get_file_content("acme/service", "app/main.py", ref="a" * 40)
        assert content == "print('ok')\n"

        clone_url = client.build_authenticated_clone_url("https://github.com/acme/service.git")
        assert "x-access-token:" in clone_url

        with pytest.raises(ResourceNotFoundError):
            client.get_repository("acme/missing")

        # Safety invariant: no merge or deploy methods exist on GitHubClient
        assert not hasattr(client, "merge_pull_request")
        assert not hasattr(client, "deploy")


def test_github_client_app_mode_exchanges_jwt_for_installation_token(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "github_app.pem"
    key_file.write_bytes(pem_bytes)

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        GITHUB_AUTH_MODE="app",
        GITHUB_APP_ID="12345",
        GITHUB_APP_INSTALLATION_ID="98765",
        GITHUB_APP_PRIVATE_KEY_PATH=str(key_file),
    )

    install_token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal install_token_calls
        if request.url.path == "/app/installations/98765/access_tokens":
            install_token_calls += 1
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx.Response(
                201,
                json={
                    "token": "ghs_installation_token_abc123",
                    "expires_at": "2030-01-01T00:00:00Z",
                },
            )
        if request.url.path == "/repos/acme/private-api":
            assert request.headers["Authorization"] == "Bearer ghs_installation_token_abc123"
            return httpx.Response(
                200,
                json={
                    "full_name": "acme/private-api",
                    "name": "private-api",
                    "default_branch": "main",
                    "clone_url": "https://github.com/acme/private-api.git",
                    "private": True,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as mock_http:
        client = GitHubClient(settings=settings, http_client=mock_http)
        repo1 = client.get_repository("acme/private-api")
        repo2 = client.get_repository("acme/private-api")
        assert repo1.full_name == "acme/private-api"
        assert repo2.full_name == "acme/private-api"
        # Token should be cached across consecutive calls
        assert install_token_calls == 1


def test_workspace_service_pins_base_sha_detects_drift_and_cleans_up(tmp_path: Path) -> None:
    source_repo = tmp_path / "source_repo"
    source_repo.mkdir()
    first_sha, second_sha = _create_local_git_repo(
        source_repo,
        with_submodules=True,
        with_lfs=True,
    )

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        WORKSPACE_ROOT_DIR=tmp_path / "workspaces",
    )
    service = WorkspaceService(settings=settings)

    info = service.prepare_workspace(
        workspace_id="wf_run_001",
        clone_source=str(source_repo),
        base_commit_sha=first_sha,
    )
    assert info.base_commit_sha == first_sha
    assert info.has_submodules is True
    assert info.has_git_lfs is True
    assert len(info.warnings) == 2

    # Base SHA verification passes for pinned SHA and fails on mismatch
    assert service.verify_base_sha(workspace_id="wf_run_001", expected_base_sha=first_sha) == (
        first_sha
    )
    with pytest.raises(ConflictError):
        service.verify_base_sha(workspace_id="wf_run_001", expected_base_sha=second_sha)

    # Stale base detection catches branch advancement
    with pytest.raises(ConflictError):
        service.detect_stale_base(
            expected_base_sha=first_sha,
            current_remote_sha=second_sha,
            branch_name="main",
        )

    # Rejects path traversal workspace identifiers
    with pytest.raises(ValidationFailedError):
        service.prepare_workspace(
            workspace_id="../escape",
            clone_source=str(source_repo),
            base_commit_sha=first_sha,
        )

    # Cleanup removes workspace cleanly
    assert service.cleanup_workspace("wf_run_001") is True
    assert not Path(info.workspace_path).exists()
    assert service.cleanup_workspace("wf_run_001") is False