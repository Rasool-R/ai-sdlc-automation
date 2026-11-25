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
        SANDBOX_SECCOMP_PROFILE="/etc/sandbox/seccomp.json",
    )
    spy_runtime = _SpyContainerRuntime()
    app = create_sandbox_app(settings=settings, runtime=spy_runtime)
    client = TestClient(app)

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

    pre_exec = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest"},
        headers=headers,
    )
    assert pre_exec.status_code == 422

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

    test_resp = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest", "target_paths": ["tests"], "pytest_args": ["-q"]},
        headers=headers,
    )
    assert test_resp.status_code == 200
    assert test_resp.json()["network_policy"] == "none"

    # Static analysis templates must run offline and enforce platform security policy
    for template in ("ruff_check", "mypy_check", "bandit_check"):
        resp = client.post(
            f"/runs/{run_id}/execute",
            json={"template": template, "target_paths": ["app"]},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "isolated_execution"
        assert resp.json()["network_policy"] == "none"

    install_argv = spy_runtime.recorded_argvs[0]
    pytest_argv = spy_runtime.recorded_argvs[1]
    ruff_argv = spy_runtime.recorded_argvs[2]
    mypy_argv = spy_runtime.recorded_argvs[3]
    bandit_argv = spy_runtime.recorded_argvs[4]

    assert install_argv[install_argv.index("--network") + 1] == "egress_restricted"
    for offline_argv in (pytest_argv, ruff_argv, mypy_argv, bandit_argv):
        assert offline_argv[offline_argv.index("--network") + 1] == "none"

    # Ruff enforces --isolated and JSON output so repo config cannot disable security rules
    assert "--isolated" in ruff_argv
    assert "--output-format" in ruff_argv
    assert "json" in ruff_argv
    assert "--show-error-codes" in mypy_argv
    assert "-f" in bandit_argv and "json" in bandit_argv

    for argv in spy_runtime.recorded_argvs:
        assert "--cap-drop=ALL" in argv
        assert "--security-opt=no-new-privileges:true" in argv
        assert "--security-opt=seccomp=/etc/sandbox/seccomp.json" in argv
        assert "--read-only" in argv
        assert "65534:65534" in argv
        assert any(arg.endswith(":/workspace:ro") for arg in argv)
        assert not any("docker.sock" in arg for arg in argv)

    results_resp = client.get(f"/runs/{run_id}/results", headers=headers)
    assert results_resp.status_code == 200
    assert len(results_resp.json()["executions"]) == 5

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

    tarslip_archive = _build_tar_gz({"../../escape.py": b"evil = True\n"})
    assert client.post(f"/runs/{run_id}/workspace", content=tarslip_archive).status_code == 422

    env_archive = _build_tar_gz({".env": b"SECRET=123\n"})
    assert client.post(f"/runs/{run_id}/workspace", content=env_archive).status_code == 422

    sym_buf = io.BytesIO()
    with tarfile.open(fileobj=sym_buf, mode="w:gz") as tar:
        sym = tarfile.TarInfo(name="link.py")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/passwd"
        tar.addfile(sym)
    assert client.post(f"/runs/{run_id}/workspace", content=sym_buf.getvalue()).status_code == 422

    clean_archive = _build_tar_gz({"tests/test_a.py": b"def test_a(): assert True\n"})
    assert client.post(f"/runs/{run_id}/workspace", content=clean_archive).status_code == 200

    bad_flag = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest", "pytest_args": ["-o", "addopts=--evil"]},
    )
    assert bad_flag.status_code == 422

    bad_path = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "pytest", "target_paths": ["tests; rm -rf /"]},
    )
    assert bad_path.status_code == 422

    bad_pkg = client.post(
        f"/runs/{run_id}/execute",
        json={"template": "install_deps", "extra_pip_packages": ["git+https://evil.test/x.git"]},
    )
    assert bad_pkg.status_code == 422

    client.delete(f"/runs/{run_id}")