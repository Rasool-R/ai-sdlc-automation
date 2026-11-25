"""Deterministic static-analysis orchestration and finding normalization.

Execution trust boundaries:
- Ruff, Mypy, and Bandit execute strictly inside the isolated sandbox with network=none.
- pip-audit requires vulnerability-database access, so it executes outside the untrusted
  container via a controlled runner against statically extracted pinned dependencies only;
  untrusted repository code or setup hooks are never executed.
"""

import json
import re
import shutil
import subprocess  # noqa: S404  # nosec B404
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.core.logging import redact_text
from app.execution.sandbox_client import (
    SandboxClient,
    SandboxExecutionResult,
    SandboxStaticAnalysisRawReport,
)
from app.intelligence.ast_parser import PythonAstParser

STATIC_ANALYSIS_SCHEMA_VERSION = "1.0"

StaticToolName = Literal["ruff", "mypy", "bandit", "pip-audit"]
NormalizedSeverity = Literal["Critical", "High", "Medium", "Low", "Informational"]
NormalizedConfidence = Literal["high", "medium", "low"]
ToolRunStatus = Literal["passed", "findings", "error", "unavailable", "skipped"]

_MYPY_LINE_PATTERN = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    r"(?P<level>error|warning|note):\s*(?P<msg>.+?)(?:\s+\[(?P<code>[^\]]+)\])?$"
)
_EXACT_PIN_PATTERN = re.compile(r"^==\s*([A-Za-z0-9][A-Za-z0-9._+-]*)$")
_SAFE_PKG_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class NormalizedFinding(BaseModel):
    """Unified schema for deterministic static-analysis and vulnerability findings."""

    tool: StaticToolName
    rule_id: str = Field(min_length=1, max_length=128)
    severity: NormalizedSeverity
    file_path: str = Field(min_length=1, max_length=512)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1, max_length=2000)
    confidence: NormalizedConfidence = "high"
    source_kind: Literal["deterministic_tool"] = "deterministic_tool"


class ToolExecutionSummary(BaseModel):
    """Captured execution metadata for a single static-analysis tool."""

    tool: StaticToolName
    status: ToolRunStatus
    exit_code: int | None = None
    tool_version: str | None = None
    duration_seconds: float = Field(default=0.0, ge=0.0)
    findings_count: int = Field(default=0, ge=0)
    network_policy: Literal["none", "controlled_advisory_egress", "not_executed"] = "none"
    notes: str = ""


class StaticAnalysisReport(BaseModel):
    """Aggregated deterministic quality-gate report across Ruff, Mypy, Bandit, and pip-audit."""

    schema_version: str = STATIC_ANALYSIS_SCHEMA_VERSION
    workflow_id: str
    repository_id: str
    base_commit_sha: str
    passed: bool
    blocking_findings_count: int = Field(default=0, ge=0)
    total_findings_count: int = Field(default=0, ge=0)
    tools: list[ToolExecutionSummary] = Field(default_factory=list)
    findings: list[NormalizedFinding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary_text: str = ""


@dataclass(frozen=True)
class PipAuditRawOutcome:
    """Raw output from a controlled pip-audit execution."""

    available: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    tool_version: str | None = None


class PipAuditRunner(Protocol):
    """Protocol abstracting controlled pip-audit execution for deterministic testing."""

    def audit_requirements_file(self, requirements_path: Path) -> PipAuditRawOutcome:
        """Run pip-audit against a platform-generated pinned requirements file."""


class SubprocessPipAuditRunner:
    """Runs pip-audit outside untrusted containers with --disable-pip and --no-deps."""

    def __init__(self, *, binary: str = "pip-audit", timeout_seconds: int = 120) -> None:
        self._binary = binary
        self._timeout = timeout_seconds

    def _probe_version(self, resolved_bin: str) -> str | None:
        try:
            proc = subprocess.run(  # noqa: S603  # nosec B603
                [resolved_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            out = (proc.stdout or proc.stderr or "").strip()
            return out.splitlines()[0][:64] if out else None
        except (OSError, subprocess.SubprocessError):
            return None

    def audit_requirements_file(self, requirements_path: Path) -> PipAuditRawOutcome:
        """Invoke pip-audit safely without executing repository build hooks."""
        resolved_bin = shutil.which(self._binary)
        if not resolved_bin:
            return PipAuditRawOutcome(
                available=False,
                exit_code=None,
                stdout="",
                stderr=f"Binary '{self._binary}' not found on controlled audit host.",
                duration_seconds=0.0,
                tool_version=None,
            )

        version = self._probe_version(resolved_bin)
        argv = [
            resolved_bin,
            "-r",
            str(requirements_path),
            "--format",
            "json",
            "--no-deps",
            "--disable-pip",
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603  # nosec B603
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
            duration = round(max(0.0, time.monotonic() - started), 4)
            return PipAuditRawOutcome(
                available=True,
                exit_code=int(completed.returncode),
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                duration_seconds=duration,
                tool_version=version,
            )
        except subprocess.TimeoutExpired:
            duration = round(max(0.0, time.monotonic() - started), 4)
            return PipAuditRawOutcome(
                available=True,
                exit_code=124,
                stdout="",
                stderr=f"pip-audit timed out after {self._timeout}s.",
                duration_seconds=duration,
                tool_version=version,
            )
        except OSError as exc:
            duration = round(max(0.0, time.monotonic() - started), 4)
            return PipAuditRawOutcome(
                available=True,
                exit_code=1,
                stdout="",
                stderr=f"pip-audit invocation error: {type(exc).__name__}",
                duration_seconds=duration,
                tool_version=version,
            )


def _normalize_workspace_path(raw_path: str) -> str:
    cleaned = raw_path.replace("\\", "/").strip()
    for prefix in ("/workspace/", "./"):
        while cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.lstrip("/") or "."


def _ruff_severity(rule_code: str) -> NormalizedSeverity:
    upper = rule_code.upper()
    if upper.startswith("S"):
        return "High"
    if upper.startswith(("F", "B")):
        return "Medium"
    return "Low"


def parse_ruff_json(stdout: str) -> tuple[list[NormalizedFinding], bool]:
    """Parse Ruff JSON output into normalized findings; return (findings, parsed_ok)."""
    stripped = stdout.strip()
    if not stripped:
        return [], True
    try:
        payload = json.loads(stripped)
    except ValueError:
        return [], False
    if not isinstance(payload, list):
        return [], False

    findings: list[NormalizedFinding] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "RUFF").strip()
        message = redact_text(str(item.get("message") or "Ruff diagnostic")).strip()
        filename = _normalize_workspace_path(str(item.get("filename") or "."))
        loc = item.get("location")
        end_loc = item.get("end_location")
        start_line = (
            int(loc["row"])
            if isinstance(loc, dict) and isinstance(loc.get("row"), int) and loc["row"] >= 1
            else None
        )
        end_line = (
            int(end_loc["row"])
            if isinstance(end_loc, dict)
            and isinstance(end_loc.get("row"), int)
            and end_loc["row"] >= 1
            else start_line
        )
        findings.append(
            NormalizedFinding(
                tool="ruff",
                rule_id=code,
                severity=_ruff_severity(code),
                file_path=filename,
                start_line=start_line,
                end_line=end_line,
                message=message[:2000],
                confidence="high",
            )
        )
    return findings, True


def parse_mypy_output(stdout: str) -> list[NormalizedFinding]:
    """Parse deterministic Mypy diagnostic lines into normalized findings."""
    findings: list[NormalizedFinding] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _MYPY_LINE_PATTERN.match(line)
        if not match:
            continue
        level = match.group("level").lower()
        severity: NormalizedSeverity = (
            "Medium" if level == "error" else "Low" if level == "warning" else "Informational"
        )
        line_num = int(match.group("line"))
        code = (match.group("code") or f"mypy-{level}").strip()
        findings.append(
            NormalizedFinding(
                tool="mypy",
                rule_id=code,
                severity=severity,
                file_path=_normalize_workspace_path(match.group("path")),
                start_line=line_num if line_num >= 1 else None,
                end_line=line_num if line_num >= 1 else None,
                message=redact_text(match.group("msg").strip())[:2000],
                confidence="high",
            )
        )
    return findings


def _map_bandit_severity(raw: str) -> NormalizedSeverity:
    upper = raw.strip().upper()
    if upper == "HIGH":
        return "High"
    if upper == "MEDIUM":
        return "Medium"
    return "Low"


def _map_bandit_confidence(raw: str) -> NormalizedConfidence:
    lower = raw.strip().lower()
    if lower in {"high", "medium", "low"}:
        return lower  # type: ignore[return-value]
    return "medium"


def parse_bandit_json(stdout: str) -> tuple[list[NormalizedFinding], bool]:
    """Parse Bandit JSON output into normalized security findings."""
    stripped = stdout.strip()
    if not stripped:
        return [], True
    try:
        payload = json.loads(stripped)
    except ValueError:
        return [], False
    if not isinstance(payload, dict):
        return [], False

    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        return [], False

    findings: list[NormalizedFinding] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("test_id") or "BANDIT").strip()
        filename = _normalize_workspace_path(str(item.get("filename") or "."))
        raw_line = item.get("line_number")
        start_line = int(raw_line) if isinstance(raw_line, int) and raw_line >= 1 else None
        line_range = item.get("line_range")
        end_line = start_line
        if isinstance(line_range, list) and line_range:
            ints = [v for v in line_range if isinstance(v, int) and v >= 1]
            if ints:
                start_line = min(ints)
                end_line = max(ints)

        findings.append(
            NormalizedFinding(
                tool="bandit",
                rule_id=rule_id,
                severity=_map_bandit_severity(str(item.get("issue_severity", "MEDIUM"))),
                file_path=filename,
                start_line=start_line,
                end_line=end_line,
                message=redact_text(str(item.get("issue_text") 
                                        or "Bandit security finding"))[:2000],
                confidence=_map_bandit_confidence(str(item.get("issue_confidence", "MEDIUM"))),
            )
        )
    return findings, True


def parse_pip_audit_json(
    stdout: str,
    *,
    source_by_package: dict[str, str],
) -> tuple[list[NormalizedFinding], bool]:
    """Parse pip-audit JSON output into normalized dependency vulnerability findings."""
    stripped = stdout.strip()
    if not stripped:
        return [], True
    try:
        payload: Any = json.loads(stripped)
    except ValueError:
        return [], False

    raw_deps: Any
    if isinstance(payload, dict):
        raw_deps = payload.get("dependencies", [])
    elif isinstance(payload, list):
        raw_deps = payload
    else:
        return [], False

    if not isinstance(raw_deps, list):
        return [], False

    findings: list[NormalizedFinding] = []
    for dep in raw_deps:
        if not isinstance(dep, dict):
            continue
        pkg_name = str(dep.get("name") or "unknown").strip()
        pkg_version = str(dep.get("version") or "unknown").strip()
        vulns = dep.get("vulns", [])
        if not isinstance(vulns, list):
            continue
        manifest_path = source_by_package.get(pkg_name.lower(), "pyproject.toml")
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            vuln_id = str(vuln.get("id") or "VULN").strip()
            aliases = vuln.get("aliases", [])
            alias_text = (
                f" ({', '.join(str(a) for a in aliases[:3])})"
                if isinstance(aliases, list) and aliases
                else ""
            )
            fix_versions = vuln.get("fix_versions", [])
            fix_text = (
                f" Fix versions: {', '.join(str(v) for v in fix_versions[:3])}."
                if isinstance(fix_versions, list) and fix_versions
                else ""
            )
            description = str(vuln.get("description") or "Known vulnerability detected.").strip()
            message = redact_text(
                f"{pkg_name}=={pkg_version} affected by {vuln_id}{alias_text}: "
                f"{description[:400]}{fix_text}"
            )
            findings.append(
                NormalizedFinding(
                    tool="pip-audit",
                    rule_id=vuln_id,
                    severity="High",
                    file_path=manifest_path,
                    start_line=None,
                    end_line=None,
                    message=message[:2000],
                    confidence="high",
                )
            )
    return findings, True


def _extract_pinned_dependencies_statically(
    workspace_dir: Path,
    parser: PythonAstParser,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Extract strictly validated pinned deps (name, version, source_file) without code exec."""
    discovered, _ = parser.discovery_service.discover(workspace_dir)
    deps = parser.extract_dependencies(workspace_dir, discovered)

    pinned: list[tuple[str, str, str]] = []
    unpinned: list[str] = []
    seen_pins: set[tuple[str, str]] = set()

    for dep in deps:
        if not _SAFE_PKG_NAME_PATTERN.match(dep.name):
            continue
        # Strip environment markers like '; python_version >= "3.11"'
        spec_base = dep.specifier.split(";", 1)[0].strip()
        pin_match = _EXACT_PIN_PATTERN.match(spec_base)
        if pin_match:
            version = pin_match.group(1)
            key = (dep.name.lower(), version)
            if key not in seen_pins:
                seen_pins.add(key)
                pinned.append((dep.name, version, dep.source_file))
        else:
            unpinned.append(f"{dep.name}{spec_base}")

    return pinned, sorted(set(unpinned))


def format_static_analysis_summary(report: StaticAnalysisReport) -> str:
    """Format a compact, secret-safe summary for CodeReviewAgent and ReleaseAgent."""
    lines: list[str] = [
        f"Static Analysis Gate Passed: {report.passed}",
        f"Total Findings: {report.total_findings_count} "
        f"(Blocking: {report.blocking_findings_count})",
    ]
    for tool_sum in report.tools:
        ver_str = f" ({tool_sum.tool_version})" if tool_sum.tool_version else ""
        lines.append(
            f"- {tool_sum.tool}{ver_str}: status={tool_sum.status}, "
            f"exit_code={tool_sum.exit_code}, findings={tool_sum.findings_count}, "
            f"network={tool_sum.network_policy}"
        )
    if report.findings:
        lines.append("Top Normalized Findings:")
        for finding in report.findings[:25]:
            loc = f":{finding.start_line}" if finding.start_line is not None else ""
            lines.append(
                f"  - [{finding.tool}][{finding.severity}][{finding.rule_id}] "
                f"{finding.file_path}{loc}: {finding.message[:200]}"
            )
    if report.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in report.warnings[:10])
    return "\n".join(lines)


class StaticAnalysisService:
    """Runs deterministic Ruff, Mypy, Bandit (in sandbox) and pip-audit (controlled host)."""

    def __init__(
        self,
        *,
        sandbox_client: SandboxClient,
        pip_audit_runner: PipAuditRunner | None = None,
        ast_parser: PythonAstParser | None = None,
    ) -> None:
        self._sandbox = sandbox_client
        self._pip_audit = pip_audit_runner or SubprocessPipAuditRunner()
        self._parser = ast_parser or PythonAstParser()

    def _normalize_sandbox_tool(
        self,
        *,
        tool: Literal["ruff", "mypy", "bandit"],
        exec_res: SandboxExecutionResult,
    ) -> tuple[ToolExecutionSummary, list[NormalizedFinding], list[str]]:
        warnings: list[str] = []
        if exec_res.timed_out:
            warnings.append(f"{tool} timed out inside sandbox.")
            return (
                ToolExecutionSummary(
                    tool=tool,
                    status="error",
                    exit_code=exec_res.exit_code,
                    duration_seconds=exec_res.duration_seconds,
                    findings_count=0,
                    network_policy="none",
                    notes="Timed out inside isolated sandbox.",
                ),
                [],
                warnings,
            )

        if tool == "ruff":
            findings, parsed_ok = parse_ruff_json(exec_res.stdout)
        elif tool == "bandit":
            findings, parsed_ok = parse_bandit_json(exec_res.stdout)
        else:
            findings = parse_mypy_output(exec_res.stdout)
            parsed_ok = True

        if not parsed_ok:
            warnings.append(f"Failed to parse structured JSON output from {tool}.")
            return (
                ToolExecutionSummary(
                    tool=tool,
                    status="error",
                    exit_code=exec_res.exit_code,
                    duration_seconds=exec_res.duration_seconds,
                    findings_count=0,
                    network_policy="none",
                    notes=redact_text(exec_res.stderr[:300] or "Malformed tool output."),
                ),
                [],
                warnings,
            )

        if exec_res.exit_code == 0 and not findings:
            status: ToolRunStatus = "passed"
        elif findings:
            status = "findings"
        else:
            status = "error"
            warnings.append(
                f"{tool} exited with code {exec_res.exit_code} without structured findings."
            )

        summary = ToolExecutionSummary(
            tool=tool,
            status=status,
            exit_code=exec_res.exit_code,
            duration_seconds=exec_res.duration_seconds,
            findings_count=len(findings),
            network_policy="none",
            notes=redact_text(exec_res.stderr[:300]) if status == "error" else "",
        )
        return summary, findings, warnings

    def _run_controlled_pip_audit(
        self,
        workspace_dir: Path,
    ) -> tuple[ToolExecutionSummary, list[NormalizedFinding], list[str]]:
        warnings: list[str] = []
        pinned, unpinned = _extract_pinned_dependencies_statically(workspace_dir, self._parser)
        if unpinned:
            warnings.append(
                f"pip-audit skipped {len(unpinned)} unpinned dependency specifier(s): "
                f"{', '.join(unpinned[:5])}."
            )

        if not pinned:
            return (
                ToolExecutionSummary(
                    tool="pip-audit",
                    status="skipped",
                    exit_code=None,
                    findings_count=0,
                    network_policy="not_executed",
                    notes="No statically pinned (==) dependencies found in repository manifests.",
                ),
                [],
                warnings,
            )

        source_by_pkg = {name.lower(): src for name, _ver, src in pinned}
        req_lines = [f"{name}=={ver}" for name, ver, _src in pinned]

        with tempfile.TemporaryDirectory(prefix="sdlc_pip_audit_") as tmp_dir:
            sanitized_req = Path(tmp_dir) / "pinned-requirements.txt"
            sanitized_req.write_text("\n".join(req_lines) + "\n", encoding="utf-8")
            outcome = self._pip_audit.audit_requirements_file(sanitized_req)

        if not outcome.available:
            warnings.append(
                "pip-audit binary is unavailable on host; vulnerability audit was not executed."
            )
            return (
                ToolExecutionSummary(
                    tool="pip-audit",
                    status="unavailable",
                    exit_code=None,
                    tool_version=None,
                    duration_seconds=outcome.duration_seconds,
                    findings_count=0,
                    network_policy="not_executed",
                    notes=redact_text(outcome.stderr[:300]),
                ),
                [],
                warnings,
            )

        findings, parsed_ok = parse_pip_audit_json(
            outcome.stdout,
            source_by_package=source_by_pkg,
        )
        if not parsed_ok:
            warnings.append("Failed to parse pip-audit JSON output.")
            return (
                ToolExecutionSummary(
                    tool="pip-audit",
                    status="error",
                    exit_code=outcome.exit_code,
                    tool_version=outcome.tool_version,
                    duration_seconds=outcome.duration_seconds,
                    findings_count=0,
                    network_policy="controlled_advisory_egress",
                    notes=redact_text(outcome.stderr[:300] or "Malformed pip-audit JSON."),
                ),
                [],
                warnings,
            )

        if outcome.exit_code == 0 and not findings:
            status: ToolRunStatus = "passed"
        elif findings:
            status = "findings"
        else:
            status = "error"
            warnings.append(f"pip-audit exited with code {outcome.exit_code}.")

        return (
            ToolExecutionSummary(
                tool="pip-audit",
                status=status,
                exit_code=outcome.exit_code,
                tool_version=outcome.tool_version,
                duration_seconds=outcome.duration_seconds,
                findings_count=len(findings),
                network_policy="controlled_advisory_egress",
                notes=redact_text(outcome.stderr[:300]) if status == "error" else "",
            ),
            findings,
            warnings,
        )

    def run(
        self,
        *,
        workflow_id: str,
        repository_id: str,
        base_commit_sha: str,
        workspace_dir: Path,
        target_paths: list[str] | None = None,
        install_dependencies: bool = True,
    ) -> StaticAnalysisReport:
        """Execute all deterministic analyzers and return a normalized quality-gate report."""
        raw_sandbox: SandboxStaticAnalysisRawReport = self._sandbox.run_static_analysis(
            workflow_id=workflow_id,
            repository_id=repository_id,
            base_commit_sha=base_commit_sha,
            workspace_dir=workspace_dir,
            target_paths=target_paths,
            install_dependencies=install_dependencies,
        )

        tools: list[ToolExecutionSummary] = []
        findings: list[NormalizedFinding] = []
        warnings: list[str] = []

        if (
            raw_sandbox.install_result is not None
            and raw_sandbox.install_result.exit_code != 0
        ):
            warnings.append(
                "Sandbox dependency installation failed before offline static analysis."
            )
            for tool_name in ("ruff", "mypy", "bandit"):
                tools.append(
                    ToolExecutionSummary(
                        tool=tool_name,
                        status="error",
                        exit_code=raw_sandbox.install_result.exit_code,
                        duration_seconds=0.0,
                        findings_count=0,
                        network_policy="not_executed",
                        notes="Skipped due to sandbox dependency install failure.",
                    )
                )
        else:
            for tool_name in ("ruff", "mypy", "bandit"):
                exec_res = raw_sandbox.tool_results.get(tool_name)
                if exec_res is None:
                    tools.append(
                        ToolExecutionSummary(
                            tool=tool_name,
                            status="error",
                            exit_code=None,
                            network_policy="not_executed",
                            notes="Tool execution result missing from sandbox runner.",
                        )
                    )
                    continue
                summary, tool_findings, tool_warnings = self._normalize_sandbox_tool(
                    tool=tool_name,  # type: ignore[arg-type]
                    exec_res=exec_res,
                )
                tools.append(summary)
                findings.extend(tool_findings)
                warnings.extend(tool_warnings)

        audit_summary, audit_findings, audit_warnings = self._run_controlled_pip_audit(
            workspace_dir
        )
        tools.append(audit_summary)
        findings.extend(audit_findings)
        warnings.extend(audit_warnings)

        blocking_findings = [
            f for f in findings if f.severity in {"Critical", "High"} or f.tool == "mypy"
        ]
        # Only actual mypy errors block (not informational notes)
        blocking_findings = [
            f
            for f in findings
            if f.severity in {"Critical", "High"}
            or (f.tool == "mypy" and f.severity == "Medium")
        ]
        has_tool_errors = any(t.status == "error" for t in tools)
        passed = (len(blocking_findings) == 0) and (not has_tool_errors)

        report = StaticAnalysisReport(
            workflow_id=workflow_id,
            repository_id=repository_id,
            base_commit_sha=base_commit_sha,
            passed=passed,
            blocking_findings_count=len(blocking_findings),
            total_findings_count=len(findings),
            tools=tools,
            findings=findings,
            warnings=warnings,
        )
        return report.model_copy(update={"summary_text": format_static_analysis_summary(report)})