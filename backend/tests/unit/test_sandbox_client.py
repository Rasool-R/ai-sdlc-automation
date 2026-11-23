"""Unit tests for workspace archive packaging and backend SandboxClient lifecycle."""

import io
import json
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest
from app.core.config import load_settings
from app.core.exceptions import AppError
from app.execution.sandbox_client import SandboxClient, package_workspace_archive
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_FIXTURE_DIR = REPO_ROOT / "fixtures" / "demo_fastapi_repo"


def test_package_workspace_archive_excludes_git_and_secret_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shutil.copytree(DEMO_FIXTURE_DIR, ws)
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (ws / ".env").write_text("APP_SECRET_KEY=super-secret\n", encoding="utf-8")
    (ws / "private.pem").write_text("PRIVATE KEY\n", encoding="utf-8")

    archive_bytes = package_workspace_archive(ws)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        names = set(tar.getnames())

    assert "app/main.py" in names
    assert "tests/test_items.py" in names
    assert "pyproject.toml" in names
    assert not any(name.startswith(".git") for name in names)
    assert ".env" not in names
    assert "private.pem" not in names


def test_sandbox_client_runs_two_phase_tests_and_always_destroys_run(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shutil.copytree(DEMO_FIXTURE_DIR, ws)

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )

    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Sandbox-Token") == "shared-token-123"
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            events.append("create")
            return httpx.Response(
                201,
                json={
                    "run_id": "run-abc",
                    "status": "created",
                    "image": "python:3.11-slim",
                    "created_at": "2026-11-23T19:00:00Z",
                },
            )
        if request.method == "POST" and path == "/runs/run-abc/workspace":
            events.append("upload")
            assert len(request.content) > 0
            return httpx.Response(
                200,
                json={
                    "run_id": "run-abc",
                    "status": "ready",
                    "files_extracted": 7,
                    "total_bytes": len(request.content),
                },
            )
        if request.method == "POST" and path == "/runs/run-abc/execute":
            body = json.loads(request.content.decode("utf-8"))
            template = body["template"]
            events.append(f"exec:{template}")
            if template == "install_deps":
                return httpx.Response(
                    200,
                    json={
                        "execution_id": "e1",
                        "template": "install_deps",
                        "phase": "dependency_install",
                        "network_policy": "restricted_egress",
                        "command_argv": ["python", "-c", "..."],
                        "exit_code": 0,
                        "stdout": "installed",
                        "stderr": "",
                        "duration_seconds": 1.2,
                        "timed_out": False,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "execution_id": "e2",
                    "template": "pytest",
                    "phase": "isolated_execution",
                    "network_policy": "none",
                    "command_argv": ["/sandbox_venv/bin/python", "-m", "pytest", "-q", "tests"],
                    "exit_code": 0,
                    "stdout": "3 passed in 0.20s",
                    "stderr": "",
                    "duration_seconds": 0.2,
                    "timed_out": False,
                },
            )
        if request.method == "DELETE" and path == "/runs/run-abc":
            events.append("destroy")
            return httpx.Response(200, json={"run_id": "run-abc", "destroyed": True})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        client = SandboxClient(
            settings=settings,
            http_client=mock_http,
            internal_token="shared-token-123",  # noqa: S106
        )
        report = client.run_isolated_tests(
            workflow_id="wf-100",
            repository_id="repo-100",
            base_commit_sha="c" * 40,
            workspace_dir=ws,
        )

    assert report.passed is True
    assert report.install_result is not None
    assert report.install_result.network_policy == "restricted_egress"
    assert report.test_result is not None
    assert report.test_result.network_policy == "none"
    assert events == ["create", "upload", "exec:install_deps", "exec:pytest", "destroy"]


def test_sandbox_client_destroys_run_even_when_execution_errors(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shutil.copytree(DEMO_FIXTURE_DIR, ws)

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )
    destroyed = False

    def failing_handler(request: httpx.Request) -> httpx.Response:
        nonlocal destroyed
        if request.method == "POST" and request.url.path == "/runs":
            return httpx.Response(201, json={"run_id": "run-fail", "status": "created"})
        if request.method == "POST" and request.url.path == "/runs/run-fail/workspace":
            return httpx.Response(500, json={"detail": "disk error"})
        if request.method == "DELETE" and request.url.path == "/runs/run-fail":
            destroyed = True
            return httpx.Response(200, json={"run_id": "run-fail", "destroyed": True})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(failing_handler)) as mock_http:
        client = SandboxClient(settings=settings, http_client=mock_http)
        with pytest.raises(AppError):
            client.run_isolated_tests(
                workflow_id="wf-fail",
                repository_id="repo-fail",
                base_commit_sha="d" * 40,
                workspace_dir=ws,
            )

    assert destroyed is True