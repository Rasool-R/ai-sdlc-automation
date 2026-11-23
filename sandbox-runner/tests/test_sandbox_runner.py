"""Unit and security tests for the isolated sandbox-runner service and Docker flags."""

import io
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient
from sandbox_runner.config import SandboxRunnerSettings
from sandbox_runner.main import create_sandbox_app
from sandbox_runner.runtime import (
    ContainerExecutionSpec,
    RawExecutionOutcome,
    build_docker_run_argv,
)


class _SpyContainerRuntime:
    """In-memory ContainerRuntime recording specs and Docker CLI invocations."""

    def __init__(self, *, exit_codes: dict[str, int] | None = None) -> None:
        self.recorded_specs: list[ContainerExecutionSpec] = []
        self.recorded_argvs: list[list[str]] = []
        self._exit_codes = exit_codes or {}

    def execute(self, spec: ContainerExecutionSpec) -> RawExecutionOutcome:
        self.recorded_specs.append(spec)
        _name, argv = build_docker_run_argv(spec)
        self.recorded_argvs.append(argv)
        code = self._exit_codes.get(spec.template, 0)
        return RawExecutionOutcome(
            exit_code=code,
            stdout=f"ran {spec.template} with token ghp_1234567890abcdefghijklmnopqrstuv",
            stderr="" if code == 0 else "test failure",
            duration_seconds=0.12,
            timed_out=False,
        )


def _build_tar_gz(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_sandbox_runner_two_phase_network_hardening_and_cleanup(tmp_path: Path) -> None:
    settings = SandboxRunnerSettings(
        SANDBOX_RUNS_ROOT=tmp_path / "runs",
        SANDBOX_INSTALL_NETWORK="egress_restricted",
        SANDBOX_INTERNAL_TOKEN="internal-secret-token-xyz",  # noqa: S106
    )
    spy_runtime = _SpyContainerRuntime()
    app = create_sandbox_app(settings=settings, runtime=spy_runtime)
    client = TestClient(app)

    # Rejects missing or wrong internal auth token
    unauth = client.post(
        "/runs",
        json={"workflow_id": "wf-1", "repository_id": "repo-1", "base_commit_sha": "a" * 40},
    )
    assert unauth.status_code == 401

    headers = {"X-Sandbox-Token": "internal-secret-token-xyz"}
    create_resp = client.post(
        "/runs",
        json={"workflow_id": "wf-1", "repository_id": "repo-1", "base_commit_sha": "a" * 40},
        headers=headers,
    )
    assert create_resp.status_code == 201
    run_id = create_resp.json()["run_id"]

    # Executing before workspace upload is rejected
    pre_exec = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest"},
        headers=headers,
    )
    assert pre_exec.status_code == 422

    # Upload clean workspace archive
    archive = _build_tar_gz(
        {
            "app/main.py": b"def ok() -> int:\n    return 1\n",
            "tests/test_main.py": b"def test_ok() -> None:\n    assert True\n",
        }
    )
    upload_resp = client.post(
        f"/runs/{run_id}/workspace",
        content=archive,
        headers={**headers, "Content-Type": "application/gzip"},
    )
    assert upload_resp.status_code == 200
    assert upload_resp.json()["files_extracted"] == 2

    # Phase 1: install_deps uses restricted egress network
    install_resp = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "install_deps"},
        headers=headers,
    )
    assert install_resp.status_code == 200
    install_body = install_resp.json()
    assert install_body["phase"] == "dependency_install"
    assert install_body["network_policy"] == "restricted_egress"
    assert "ghp_1234567890abcdefghijklmnopqrstuv" not in install_body["stdout"]
    assert "[REDACTED]" in install_body["stdout"]

    # Phase 2: pytest runs strictly with network=none and hardened Docker flags
    test_resp = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest", "target_paths": ["tests"], "pytest_args": ["-q"]},
        headers=headers,
    )
    assert test_resp.status_code == 200
    test_body = test_resp.json()
    assert test_body["phase"] == "isolated_execution"
    assert test_body["network_policy"] == "none"

    install_argv = spy_runtime.recorded_argvs[0]
    pytest_argv = spy_runtime.recorded_argvs[1]

    assert install_argv[install_argv.index("--network") + 1] == "egress_restricted"
    assert pytest_argv[pytest_argv.index("--network") + 1] == "none"
    for argv in (install_argv, pytest_argv):
        assert "--cap-drop=ALL" in argv
        assert "--security-opt=no-new-privileges:true" in argv
        assert "--read-only" in argv
        assert "65534:65534" in argv
        assert any(arg.endswith(":/workspace:ro") for arg in argv)
        assert not any("docker.sock" in arg for arg in argv)

    # Fetch aggregated results
    results_resp = client.get(f"/runs/{run_id}/results", headers=headers)
    assert results_resp.status_code == 200
    assert len(results_resp.json()["executions"]) == 2

    # Destroy run cleans up filesystem completely
    run_dir = tmp_path / "runs" / run_id
    assert run_dir.exists()
    destroy_resp = client.delete(f"/runs/{run_id}", headers=headers)
    assert destroy_resp.status_code == 200
    assert destroy_resp.json()["destroyed"] is True
    assert not run_dir.exists()


def test_sandbox_runner_rejects_tarslip_symlinks_env_and_command_injection(
    tmp_path: Path,
) -> None:
    settings = SandboxRunnerSettings(SANDBOX_RUNS_ROOT=tmp_path / "runs")
    app = create_sandbox_app(settings=settings, runtime=_SpyContainerRuntime())
    client = TestClient(app)

    run_id = client.post(
        "/runs",
        json={"workflow_id": "wf-sec", "repository_id": "repo-sec", "base_commit_sha": "b" * 40},
    ).json()["run_id"]

    # 1. Path traversal archive rejected
    tarslip_archive = _build_tar_gz({"../../escape.py": b"evil = True\n"})
    assert client.post(f"/runs/{run_id}/workspace", content=tarslip_archive).status_code == 422

    # 2. .env secret file rejected
    env_archive = _build_tar_gz({".env": b"SECRET=123\n"})
    assert client.post(f"/runs/{run_id}/workspace", content=env_archive).status_code == 422

    # 3. Symlink entry rejected
    sym_buf = io.BytesIO()
    with tarfile.open(fileobj=sym_buf, mode="w:gz") as tar:
        sym = tarfile.TarInfo(name="link.py")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        tar.addfile(sym)
    assert client.post(f"/runs/{run_id}/workspace", content=sym_buf.getvalue()).status_code == 422

    # Upload clean workspace then test command-injection rejections
    clean_archive = _build_tar_gz({"tests/test_a.py": b"def test_a(): assert True\n"})
    assert client.post(f"/runs/{run_id}/workspace", content=clean_archive).status_code == 200

    # Non-allowlisted pytest flag rejected
    bad_flag = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest", "pytest_args": ["-o", "addopts=--evil"]},
    )
    assert bad_flag.status_code == 422

    # Shell metacharacter in target_paths rejected
    bad_path = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest", "target_paths": ["tests; rm -rf /"]},
    )
    assert bad_path.status_code == 422

    # Pip URL injection rejected
    bad_pkg = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "install_deps", "extra_pip_packages": ["git+https://evil.test/x.git"]},
    )
    assert bad_pkg.status_code == 422

    client.delete(f"/runs/{run_id}")