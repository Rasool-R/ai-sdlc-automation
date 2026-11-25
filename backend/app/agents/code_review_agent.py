"""Code Review Agent combining structured AI review with deterministic safety gating."""

from dataclasses import dataclass

from app.agents.schemas import (
    CodeReviewReport,
    ImplementationPlan,
    RequirementSpecification,
)
from app.execution.sandbox_client import SandboxRunReport
from app.execution.schemas import PatchValidationResult
from app.execution.static_analysis import StaticAnalysisReport
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.prompts import load_prompt, wrap_untrusted_data

MAX_REVIEW_DIFF_CHARS = 24_000
MAX_STATIC_ANALYSIS_CHARS = 12_000


@dataclass(frozen=True)
class CodeReviewAgentResult:
    """Deterministically gated code review report paired with LLM usage telemetry."""

    report: CodeReviewReport
    usage: LLMUsageMetrics


class CodeReviewAgent:
    """Reviews raw diffs, test results, static findings, and deterministic integrity flags."""

    def __init__(self, llm_client: LLMClient, *, prompt_version: str = "v1") -> None:
        self._llm = llm_client
        self._prompt = load_prompt("code_review", prompt_version)

    def _apply_deterministic_gates(
        self,
        *,
        report: CodeReviewReport,
        patch_validation: PatchValidationResult,
        sandbox_report: SandboxRunReport | None,
        static_analysis_report: StaticAnalysisReport | None,
    ) -> CodeReviewReport:
        """Enforce non-overridable blocking rules independently of LLM judgment."""
        blocking_reasons = list(report.blocking_reasons)
        passed = report.passed

        severe_confirmed = [
            f
            for f in report.findings
            if f.severity in {"Critical", "High"} and f.classification == "confirmed_defect"
        ]
        if severe_confirmed:
            passed = False
            reason = (
                f"Code review identified {len(severe_confirmed)} Critical/High confirmed defect(s)."
            )
            if reason not in blocking_reasons:
                blocking_reasons.append(reason)

        addressed_pairs = {
            (item.file_path, item.rule_id)
            for item in report.test_integrity_assessments
            if item.addressed
        }
        unaddressed_flags = [
            flag
            for flag in patch_validation.test_integrity_flags
            if (flag.file_path, flag.rule_id) not in addressed_pairs
        ]
        if unaddressed_flags:
            passed = False
            details = ", ".join(f"{f.file_path}:{f.rule_id}" for f in unaddressed_flags)
            reason = f"Unaddressed deterministic test-integrity flag(s): {details}."
            if reason not in blocking_reasons:
                blocking_reasons.append(reason)

        if sandbox_report is not None and not sandbox_report.passed:
            passed = False
            reason = "Isolated sandbox tests have not passed."
            if reason not in blocking_reasons:
                blocking_reasons.append(reason)

        if static_analysis_report is not None and not static_analysis_report.passed:
            passed = False
            reason = (
                "Deterministic static analysis gate failed "
                f"({static_analysis_report.blocking_findings_count} blocking finding(s))."
            )
            if reason not in blocking_reasons:
                blocking_reasons.append(reason)

        return report.model_copy(
            update={
                "passed": passed,
                "blocking_reasons": blocking_reasons,
            }
        )

    def run(
        self,
        *,
        requirement: RequirementSpecification,
        plan: ImplementationPlan,
        patch_validation: PatchValidationResult,
        is_private_repo: bool,
        llm_data_sharing: bool,
        sandbox_report: SandboxRunReport | None = None,
        static_analysis_report: StaticAnalysisReport | None = None,
        static_analysis_summary: str | None = None,
    ) -> CodeReviewAgentResult:
        """Execute structured code review and enforce deterministic quality/safety gates."""
        flags_summary = {
            "files_changed": patch_validation.files_changed,
            "lines_added": patch_validation.lines_added,
            "lines_removed": patch_validation.lines_removed,
            "scope_drift_files": patch_validation.scope_drift_files,
            "protected_path_flags": [
                f.model_dump(mode="json") for f in patch_validation.protected_path_flags
            ],
            "test_integrity_flags": [
                f.model_dump(mode="json") for f in patch_validation.test_integrity_flags
            ],
        }
        sandbox_summary = (
            sandbox_report.model_dump_json(indent=2)
            if sandbox_report is not None
            else "No sandbox test report provided."
        )
        effective_static_summary = static_analysis_summary
        if not effective_static_summary and static_analysis_report is not None:
            effective_static_summary = static_analysis_report.summary_text

        sections = [
            f"Base Commit SHA: {patch_validation.base_commit_sha}",
            f"Patch Hash: {patch_validation.patch_hash}",
            "Validated RequirementSpecification:",
            wrap_untrusted_data(
                "requirement_specification",
                requirement.model_dump_json(indent=2),
            ),
            "Approved ImplementationPlan:",
            wrap_untrusted_data(
                "implementation_plan",
                plan.model_dump_json(indent=2),
            ),
            "Deterministic Patch & Integrity Flags:",
            wrap_untrusted_data("patch_flags", str(flags_summary)),
            "Raw Unified Diff:",
            wrap_untrusted_data(
                "raw_unified_diff",
                patch_validation.unified_diff[:MAX_REVIEW_DIFF_CHARS],
            ),
            "Sandbox Test Execution Summary:",
            wrap_untrusted_data("sandbox_report", sandbox_summary[:MAX_STATIC_ANALYSIS_CHARS]),
        ]
        if effective_static_summary:
            sections.append(
                "Static Analysis Findings (Ruff / Mypy / Bandit / pip-audit):\n"
                + wrap_untrusted_data(
                    "static_analysis_findings",
                    effective_static_summary[:MAX_STATIC_ANALYSIS_CHARS],
                )
            )

        response = self._llm.complete_structured(
            system_prompt=self._prompt.content,
            user_prompt="\n\n".join(sections),
            response_model=CodeReviewReport,
            prompt_version=self._prompt.version,
            is_private_repo=is_private_repo,
            llm_data_sharing=llm_data_sharing,
        )

        gated_report = self._apply_deterministic_gates(
            report=response.parsed,
            patch_validation=patch_validation,
            sandbox_report=sandbox_report,
            static_analysis_report=static_analysis_report,
        )
        return CodeReviewAgentResult(report=gated_report, usage=response.usage)