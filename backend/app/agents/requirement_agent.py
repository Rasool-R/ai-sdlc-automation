"""Requirement Analysis Agent producing validated RequirementSpecification contracts."""

from dataclasses import dataclass

from app.agents.schemas import RequirementSpecification
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.prompts import load_prompt, wrap_untrusted_data


@dataclass(frozen=True)
class RequirementAgentResult:
    """Validated requirement specification paired with LLM usage telemetry."""

    specification: RequirementSpecification
    usage: LLMUsageMetrics


class RequirementAnalysisAgent:
    """Transforms a raw feature request or bug report into testable requirements."""

    def __init__(self, llm_client: LLMClient, *, prompt_version: str = "v1") -> None:
        self._llm = llm_client
        self._prompt = load_prompt("requirement_analysis", prompt_version)

    def run(
        self,
        *,
        title: str,
        request_text: str,
        repository_full_name: str,
        is_private_repo: bool,
        llm_data_sharing: bool,
        clarification_answers: str | None = None,
    ) -> RequirementAgentResult:
        """Analyze user request and produce a validated RequirementSpecification."""
        sections = [
            f"Target repository: {repository_full_name}",
            wrap_untrusted_data("feature_title", title),
            wrap_untrusted_data("feature_request", request_text),
        ]
        if clarification_answers:
            sections.append(
                wrap_untrusted_data("operator_clarification_answers", clarification_answers)
            )

        user_prompt = "\n\n".join(sections)
        response = self._llm.complete_structured(
            system_prompt=self._prompt.content,
            user_prompt=user_prompt,
            response_model=RequirementSpecification,
            prompt_version=self._prompt.version,
            is_private_repo=is_private_repo,
            llm_data_sharing=llm_data_sharing,
        )
        return RequirementAgentResult(
            specification=response.parsed,
            usage=response.usage,
        )