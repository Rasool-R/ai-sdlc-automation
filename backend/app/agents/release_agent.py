"""Release Agent preparing evidence-backed PR metadata and final completion reports."""

from dataclasses import dataclass

from app.agents.schemas import (
    CodeReviewReport,
    ImplementationPlan,
    ReleasePreparationOutput,
    RequirementSpecification,
)
from app.execution.sandbox_client import SandboxRunReport
from app.execution.schemas import PatchValidationResult
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.prompts import load_prompt, wrap_untrusted_data

MAX_RELEASE_DIFF_CHARS = 12_000


@dataclass(frozen=True)
class ReleaseAgentResult:
    """Validated PR preparation bundle paired with LLM usage telemetry."""

    output: ReleasePreparationOutput
    usage: LLMUsageMetrics


class ReleaseAgent:
    """Drafts PR title/description, release notes, and final report without merging/deploying."""

    def __init__(self, llm_client: LLMClient, *, prompt_version: str = "v1") -> None:
        self._llm = llm_client
        self._prompt = load_prompt("release", prompt_version)

    def run(
        self,
        *,
        repository_full_name: str,
        base_branch: str,
        requirement: RequirementSpecification,
        plan: ImplementationPlan,
        patch_validation: PatchValidationResult,
        sandbox_report: SandboxRunReport,
        review_report: CodeReviewReport,
        is_private_repo: bool,
        llm_data_sharing: bool,
        static_analysis_summary: str | None = None,
    ) -> ReleaseAgentResult:
        """Prepare evidence-backed PR text and final report for human Commit/PR approval."""
        sections = [
            f"Repository: {repository_full_name}",
            f"Base Branch: {base_branch}",
            f"Base Commit SHA: {patch_validation.base_commit_sha}",
            f"Patch Hash: {patch_validation.patch_hash}",
            f"Changed Files: {', '.join(patch_validation.files_changed)}",
            f"Diff Stats: +{patch_validation.lines_added} / -{patch_validation.lines_removed}",
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
            "Unified Diff Excerpt:",
            wrap_untrusted_data(
                "raw_unified_diff",
                patch_validation.unified_diff[:MAX_RELEASE_DIFF_CHARS],
            ),
            "Verified Sandbox Test Report:",
            wrap_untrusted_data(
                "sandbox_report",
                sandbox_report.model_dump_json(indent=2),
            ),
            "Verified Code Review Report:",
            wrap_untrusted_data(
                "code_review_report",
                review_report.model_dump_json(indent=2),
            ),
        ]
        if static_analysis_summary:
            sections.append(
                "Static Analysis Summary:\n"
                + wrap_untrusted_data("static_analysis_summary", static_analysis_summary[:6000])
            )

        response = self._llm.complete_structured(
            system_prompt=self._prompt.content,
            user_prompt="\n\n".join(sections),
            response_model=ReleasePreparationOutput,
            prompt_version=self._prompt.version,
            is_private_repo=is_private_repo,
            llm_data_sharing=llm_data_sharing,
        )
        return ReleaseAgentResult(output=response.parsed, usage=response.usage)