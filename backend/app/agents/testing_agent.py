"""Testing Agent performing failure diagnosis and bounded correction recommendations."""

from dataclasses import dataclass

from app.agents.schemas import (
    ImplementationPlan,
    RequirementSpecification,
    TestFailureDiagnosis,
)
from app.execution.sandbox_client import SandboxRunReport
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.prompts import load_prompt, wrap_untrusted_data

MAX_DIFF_CHARS = 16_000
MAX_LOG_CHARS = 12_000


@dataclass(frozen=True)
class TestingAgentResult:
    """Validated failure diagnosis paired with LLM usage telemetry."""

    diagnosis: TestFailureDiagnosis
    usage: LLMUsageMetrics


class TestingAgent:
    """Diagnoses isolated sandbox test failures without ever running host commands."""

    def __init__(self, llm_client: LLMClient, *, prompt_version: str = "v1") -> None:
        self._llm = llm_client
        self._prompt = load_prompt("testing", prompt_version)

    def _format_sandbox_evidence(self, report: SandboxRunReport) -> str:
        sections: list[str] = [
            f"Sandbox Run ID: {report.run_id}",
            f"Base Commit SHA: {report.base_commit_sha}",
            f"Overall Passed: {report.passed}",
        ]
        for idx, exec_rec in enumerate(report.executions, start=1):
            stdout_trimmed = exec_rec.stdout[-MAX_LOG_CHARS:]
            stderr_trimmed = exec_rec.stderr[-MAX_LOG_CHARS:]
            sections.append(
                f"Execution #{idx} | template={exec_rec.template} | "
                f"phase={exec_rec.phase} | network={exec_rec.network_policy} | "
                f"exit_code={exec_rec.exit_code} | timed_out={exec_rec.timed_out} | "
                f"duration={exec_rec.duration_seconds}s\n"
                f"STDOUT:\n{stdout_trimmed}\n"
                f"STDERR:\n{stderr_trimmed}"
            )
        return "\n\n---\n\n".join(sections)

    def analyze_failure(
        self,
        *,
        requirement: RequirementSpecification,
        plan: ImplementationPlan,
        raw_diff: str,
        sandbox_report: SandboxRunReport,
        is_private_repo: bool,
        llm_data_sharing: bool,
        attempt_number: int = 1,
    ) -> TestingAgentResult:
        """Analyze failed sandbox test evidence and recommend bounded code corrections."""
        evidence_text = self._format_sandbox_evidence(sandbox_report)
        user_prompt = "\n\n".join(
            [
                f"Failure Analysis Attempt: {attempt_number}",
                f"Base Commit SHA: {sandbox_report.base_commit_sha}",
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
                "Applied Unified Diff (bounded):",
                wrap_untrusted_data("applied_unified_diff", raw_diff[:MAX_DIFF_CHARS]),
                "Isolated Sandbox Execution Evidence:",
                wrap_untrusted_data("sandbox_test_output", evidence_text),
            ]
        )

        response = self._llm.complete_structured(
            system_prompt=self._prompt.content,
            user_prompt=user_prompt,
            response_model=TestFailureDiagnosis,
            prompt_version=self._prompt.version,
            is_private_repo=is_private_repo,
            llm_data_sharing=llm_data_sharing,
        )

        diagnosis = response.parsed
        # Deterministically prevent blind code loops on sandbox timeouts or install env errors
        if (
            sandbox_report.install_result is not None
            and sandbox_report.install_result.exit_code != 0
            and diagnosis.failure_category == "environment_error"
        ):
            diagnosis = diagnosis.model_copy(update={"is_fixable_by_coding_agent": False})

        return TestingAgentResult(diagnosis=diagnosis, usage=response.usage)