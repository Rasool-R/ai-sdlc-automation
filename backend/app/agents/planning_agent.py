"""Consolidated Planning Agent producing validated TechnicalDesign and ImplementationPlan."""

from dataclasses import dataclass

from app.agents.schemas import (
    ImplementationPlan,
    PlanningOutput,
    RepositoryAnalysisReport,
    RequirementSpecification,
    TechnicalDesign,
)
from app.execution.patch_service import classify_protected_path
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.prompts import load_prompt, wrap_untrusted_data


@dataclass(frozen=True)
class PlanningAgentResult:
    """Validated technical design and implementation plan paired with LLM telemetry."""

    design: TechnicalDesign
    plan: ImplementationPlan
    usage: LLMUsageMetrics


class PlanningAgent:
    """Consolidated Architecture + Planning agent minimizing handoff overhead."""

    def __init__(self, llm_client: LLMClient, *, prompt_version: str = "v1") -> None:
        self._llm = llm_client
        self._prompt = load_prompt("planning", prompt_version)

    def run(
        self,
        *,
        requirement: RequirementSpecification,
        repo_analysis: RepositoryAnalysisReport,
        is_private_repo: bool,
        llm_data_sharing: bool,
    ) -> PlanningAgentResult:
        """Generate a bounded technical design and implementation plan for human approval."""
        user_prompt = "\n\n".join(
            [
                f"Base Commit SHA: {repo_analysis.base_commit_sha}",
                "Validated RequirementSpecification:",
                wrap_untrusted_data(
                    "requirement_specification",
                    requirement.model_dump_json(indent=2),
                ),
                "Validated RepositoryAnalysisReport:",
                wrap_untrusted_data(
                    "repository_analysis_report",
                    repo_analysis.model_dump_json(indent=2),
                ),
            ]
        )

        response = self._llm.complete_structured(
            system_prompt=self._prompt.content,
            user_prompt=user_prompt,
            response_model=PlanningOutput,
            prompt_version=self._prompt.version,
            is_private_repo=is_private_repo,
            llm_data_sharing=llm_data_sharing,
        )

        # Deterministically ensure all task expected_files are aggregated and protected paths noted
        all_expected: list[str] = list(dict.fromkeys(response.parsed.plan.expected_files))
        for task in response.parsed.plan.tasks:
            for path in task.expected_files:
                if path not in all_expected:
                    all_expected.append(path)

        declared_protected = list(
            dict.fromkeys(
                [
                    *response.parsed.plan.declared_protected_paths,
                    *response.parsed.design.protected_path_changes,
                ]
            )
        )
        for path in all_expected:
            if classify_protected_path(path) is not None and path not in declared_protected:
                declared_protected.append(path)

        normalized_plan = response.parsed.plan.model_copy(
            update={
                "expected_files": all_expected,
                "declared_protected_paths": declared_protected,
            }
        )
        return PlanningAgentResult(
            design=response.parsed.design,
            plan=normalized_plan,
            usage=response.usage,
        )