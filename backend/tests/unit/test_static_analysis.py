"""Unit and security regression tests for deterministic static analysis and normalization."""

import json
from pathlib import Path

import httpx
from app.core.config import load_settings
from app.execution.sandbox_client import SandboxClient
from app.execution.static_analysis import (
    PipAuditRawOutcome,
    StaticAnalysisService,
    parse_bandit_json,
    parse_mypy_output,
    parse_pip_audit_json,
    parse_ruff_json,
)
from cryptography.fernet import Fernet


class _StubPipAuditRunner:
    """Deterministic stub for controlled pip-audit execution in unit tests."""

    def __init__(self, outcome: PipAuditRawOutcome) -> None:
        self._outcome = outcome
        self.captured_requirements: list[str] = []

    def audit_requirements_file(self, requirements_path: Path) -> PipAuditRawOutcome:
        self.captured_requirements = requirements_path.read_text(encoding="utf-8").splitlines()
        return self._outcome


def _write_file(root: Path, rel_path: str, content: str) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_parsers_normalize_ruff_mypy_bandit_and_pip_audit_outputs() -> None:
    ruff_json = json.dumps(
        [
            {
                "code": "S105",
                "message": "Possible hardcoded password assigned with " \
                "token ghp_1234567890abcdefghijklmnopqrstuv",
                "filename": "/workspace/app/auth.py",
                "location": {"row": 14, "column": 5},
                "end_location": {"row": 14, "column": 35},
            },
            {
                "code": "E501",
                "message": "Line too long (105 > 100)",
                "filename": "/workspace/app/main.py",
                "location": {"row": 22, "column": 101},
                "end_location": {"row": 22, "column": 105},
            },
        ]
    )
    ruff_findings, ruff_ok = parse_ruff_json(ruff_json)
    assert ruff_ok is True
    assert len(ruff_findings) == 2
    assert ruff_findings[0].file_path == "app/auth.py"
    assert ruff_findings[0].severity == "High"
    assert ruff_findings[0].start_line == 14
    assert "ghp_1234567890abcdefghijklmnopqrstuv" not in ruff_findings[0].message
    assert ruff_findings[1].severity == "Low"

    mypy_text = (
        "app/main.py:18:12: error: Incompatible return value type  [return-value]\n"
        "app/main.py:18:12: note: Expected int\n"
    )
    mypy_findings = parse_mypy_output(mypy_text)
    assert len(mypy_findings) == 2
    assert mypy_findings[0].tool == "mypy"
    assert mypy_findings[0].rule_id == "return-value"
    assert mypy_findings[0].severity == "Medium"
    assert mypy_findings[1].severity == "Informational"

    bandit_json = json.dumps(
        {
            "results": [
                {
                    "test_id": "B307",
                    "issue_severity": "HIGH",
                    "issue_confidence": "HIGH",
                    "issue_text": "Use of possibly insecure function eval.",
                    "filename": "/workspace/app/calc.py",
                    "line_number": 9,
                    "line_range": [9, 10],
                }
            ]
        }
    )
    bandit_findings, bandit_ok = parse_bandit_json(bandit_json)
    assert bandit_ok is True
    assert len(bandit_findings) == 1
    assert bandit_findings[0].file_path == "app/calc.py"
    assert bandit_findings[0].severity == "High"
    assert bandit_findings[0].start_line == 9
    assert bandit_findings[0].end_line == 10

    pip_audit_json = json.dumps(
        {
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.19.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2018-28",
                            "aliases": ["CVE-2018-18074"],
                            "fix_versions": ["2.20.0"],
                            "description": "Session object leaks Authorization headers.",
                        }
                    ],
                }
            ]
        }
    )
    audit_findings, audit_ok = parse_pip_audit_json(
        pip_audit_json,
        source_by_package={"requests": "requirements.txt"},
    )
    assert audit_ok is True
    assert len(audit_findings) == 1
    assert audit_findings[0].tool == "pip-audit"
    assert audit_findings[0].rule_id == "PYSEC-2018-28"
    assert audit_findings[0].severity == "High"
    assert audit_findings[0].file_path == "requirements.txt"


def test_static_analysis_service_combines_sandbox_and_pip_audit_without_executing_repo_code(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    marker = tmp_path / "setup_executed.txt"
    _write_file(
        ws,
        "setup.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('PWNED', encoding='utf-8')\n",
    )
    _write_file(
        ws,
        "pyproject.toml",
        (
            "[project]\n"
            'name = "target-api"\n'
            'dependencies = ["fastapi==0.115.6", "requests==2.19.0", "pydantic>=2.10.0"]\n'
            "[tool.ruff.lint]\n"
            'ignore = ["S"]\n'
        ),
    )
    _write_file(ws, "app/main.py", "def run() -> int:\n    return 1\n")

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )

    executed_templates: list[str] = []

    def sandbox_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            return httpx.Response(201, json={"run_id": "sa-run-1", "status": "created"})
        if request.method == "POST" and path == "/runs/sa-run-1/workspace":
            return httpx.Response(200, json={"run_id": "sa-run-1", "status": "ready"})
        if request.method == "POST" and path == "/runs/sa-run-1/execute":
            body = json.loads(request.content.decode("utf-8"))
            template = body["template"]
            executed_templates.append(template)
            if template == "install_deps":
                return httpx.Response(
                    200,
                    json={
                        "execution_id": "e-inst",
                        "template": "install_deps",
                        "phase": "dependency_install",
                        "network_policy": "restricted_egress",
                        "command_argv": ["python"],
                        "exit_code": 0,
                        "stdout": "ok",
                        "stderr": "",
                        "duration_seconds": 0.4,
                        "timed_out": False,
                    },
                )
            if template == "ruff_check":
                return httpx.Response(
                    200,
                    json={
                        "execution_id": "e-ruff",
                        "template": "ruff_check",
                        "phase": "isolated_execution",
                        "network_policy": "none",
                        "command_argv": ["ruff"],
                        "exit_code": 1,
                        "stdout": json.dumps(
                            [
                                {
                                    "code": "E501",
                                    "message": "Line too long",
                                    "filename": "/workspace/app/main.py",
                                    "location": {"row": 1, "column": 101},
                                    "end_location": {"row": 1, "column": 105},
                                }
                            ]
                        ),
                        "stderr": "",
                        "duration_seconds": 0.1,
                        "timed_out": False,
                    },
                )
            if template == "mypy_check":
                return httpx.Response(
                    200,
                    json={
                        "execution_id": "e-mypy",
                        "template": "mypy_check",
                        "phase": "isolated_execution",
                        "network_policy": "none",
                        "command_argv": ["mypy"],
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "duration_seconds": 0.2,
                        "timed_out": False,
                    },
                )
            if template == "bandit_check":
                return httpx.Response(
                    200,
                    json={
                        "execution_id": "e-bandit",
                        "template": "bandit_check",
                        "phase": "isolated_execution",
                        "network_policy": "none",
                        "command_argv": ["bandit"],
                        "exit_code": 0,
                        "stdout": json.dumps({"results": []}),
                        "stderr": "",
                        "duration_seconds": 0.1,
                        "timed_out": False,
                    },
                )
        if request.method == "DELETE" and path == "/runs/sa-run-1":
            return httpx.Response(200, json={"run_id": "sa-run-1", "destroyed": True})
        return httpx.Response(404)

    pip_audit_stub = _StubPipAuditRunner(
        PipAuditRawOutcome(
            available=True,
            exit_code=1,
            stdout=json.dumps(
                {
                    "dependencies": [
                        {
                            "name": "requests",
                            "version": "2.19.0",
                            "vulns": [
                                {
                                    "id": "PYSEC-2018-28",
                                    "fix_versions": ["2.20.0"],
                                    "description": "Header leak vulnerability.",
                                }
                            ],
                        }
                    ]
                }
            ),
            stderr="",
            duration_seconds=0.3,
            tool_version="pip-audit 2.7.3",
        )
    )

    with httpx.Client(transport=httpx.MockTransport(sandbox_handler)) as mock_http:
        service = StaticAnalysisService(
            sandbox_client=SandboxClient(settings=settings, http_client=mock_http),
            pip_audit_runner=pip_audit_stub,
        )
        report = service.run(
            workflow_id="wf-sa-1",
            repository_id="repo-sa-1",
            base_commit_sha="a" * 40,
            workspace_dir=ws,
            target_paths=["app"],
        )

    # Untrusted setup.py was NEVER executed on host
    assert not marker.exists()
    # Only strictly pinned dependencies were handed to pip-audit
    assert set(pip_audit_stub.captured_requirements) == {"fastapi==0.115.6", "requests==2.19.0"}
    assert executed_templates == ["install_deps", "ruff_check", "mypy_check", "bandit_check"]
    # High severity pip-audit vulnerability blocks the gate; Low Ruff warning is non-blocking
    assert report.passed is False
    assert report.total_findings_count == 2
    assert report.blocking_findings_count == 1
    assert any("unpinned" in w for w in report.warnings)
    assert "PYSEC-2018-28" in report.summary_text


def test_static_analysis_reports_unavailable_pip_audit_honestly_and_passes_clean_code(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "clean_ws"
    _write_file(ws, "requirements.txt", "fastapi==0.115.6\n")
    _write_file(ws, "app/main.py", "def ok() -> int:\n    return 1\n")

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )

    def clean_sandbox_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            return httpx.Response(201, json={"run_id": "sa-clean", "status": "created"})
        if request.method == "POST" and path == "/runs/sa-clean/workspace":
            return httpx.Response(200, json={"run_id": "sa-clean", "status": "ready"})
        if request.method == "POST" and path == "/runs/sa-clean/execute":
            template = json.loads(request.content.decode("utf-8"))["template"]
            stdout = json.dumps({"results": []}) if template == "bandit_check" else ""
            if template == "ruff_check":
                stdout = "[]"
            return httpx.Response(
                200,
                json={
                    "execution_id": f"e-{template}",
                    "template": template,
                    "phase": (
                        "dependency_install"
                        if template == "install_deps"
                        else "isolated_execution"
                    ),
                    "network_policy": "restricted_egress" if template == "install_deps" else "none",
                    "command_argv": [template],
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                    "duration_seconds": 0.1,
                    "timed_out": False,
                },
            )
        if request.method == "DELETE" and path == "/runs/sa-clean":
            return httpx.Response(200, json={"run_id": "sa-clean", "destroyed": True})
        return httpx.Response(404)

    missing_pip_audit = _StubPipAuditRunner(
        PipAuditRawOutcome(
            available=False,
            exit_code=None,
            stdout="",
            stderr="Binary 'pip-audit' not found on controlled audit host.",
            duration_seconds=0.0,
        )
    )

    with httpx.Client(transport=httpx.MockTransport(clean_sandbox_handler)) as mock_http:
        service = StaticAnalysisService(
            sandbox_client=SandboxClient(settings=settings, http_client=mock_http),
            pip_audit_runner=missing_pip_audit,
        )
        report = service.run(
            workflow_id="wf-clean",
            repository_id="repo-clean",
            base_commit_sha="b" * 40,
            workspace_dir=ws,
        )

    assert report.passed is True
    assert report.total_findings_count == 0
    audit_tool = next(t for t in report.tools if t.tool == "pip-audit")
    assert audit_tool.status == "unavailable"
    assert audit_tool.network_policy == "not_executed"
    assert any("unavailable" in w for w in report.warnings)