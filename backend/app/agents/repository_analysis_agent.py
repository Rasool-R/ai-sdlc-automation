"""Repository Analysis Agent grounded in bounded hybrid retrieval and commit provenance."""

from dataclasses import dataclass
from pathlib import Path

from app.agents.schemas import RepositoryAnalysisReport, RequirementSpecification
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.intelligence.retrieval import HybridRetrievalService, RetrievalResponse
from app.models.repository import RepositoryModel
from app.prompts import load_prompt, wrap_untrusted_data


@dataclass(frozen=True)
class RepositoryAnalysisAgentResult:
    """Validated repository analysis paired with retrieval bundle and LLM telemetry."""

    report: RepositoryAnalysisReport
    retrieval: RetrievalResponse
    usage: LLMUsageMetrics


class RepositoryAnalysisAgent:
    """Inspects bounded retrieved repository context without dumping entire codebases."""

    def __init__(
        self,
        llm_client: LLMClient,
        retrieval_service: HybridRetrievalService,
        *,
        prompt_version: str = "v1",
    ) -> None:
        self._llm = llm_client
        self._retrieval = retrieval_service
        self._prompt = load_prompt("repository_analysis", prompt_version)

    def _format_retrieval_context(self, retrieval: RetrievalResponse) -> str:
        blocks: list[str] = [
            f"Retrieval mode: {retrieval.retrieval_mode}",
            f"Files included: {retrieval.files_included}",
            f"Estimated tokens: {retrieval.total_estimated_tokens}",
        ]
        for item in retrieval.items:
            header = (
                f"FILE: {item.file_path} | LINES: {item.start_line}-{item.end_line} | "
                f"SYMBOL: {item.qualified_name} ({item.kind}) | "
                f"TEST: {item.is_test} | SCORE: {item.score}"
            )
            blocks.append(f"{header}\n{item.excerpt}")

        if retrieval.relationships:
            rel_lines = [
                f"{r.source_symbol} -[{r.relationship_type}:{r.confidence}]-> "
                f"{r.target_symbol} ({r.file_path})"
                for r in retrieval.relationships[:40]
            ]
            blocks.append("STRUCTURAL RELATIONSHIPS:\n" + "\n".join(rel_lines))

        return "\n\n---\n\n".join(blocks)

    def run(
        self,
        *,
        repository: RepositoryModel,
        base_commit_sha: str,
        requirement: RequirementSpecification,
        workspace_dir: Path | None = None,
        hint_paths: list[str] | None = None,
        hint_symbols: list[str] | None = None,
    ) -> RepositoryAnalysisAgentResult:
        """Retrieve bounded repository context and produce a provenance-pinned report."""
        query = " ".join(
            [requirement.summary, *requirement.functional_requirements[:5]]
        )
        retrieval = self._retrieval.retrieve(
            repository=repository,
            commit_sha=base_commit_sha,
            query=query,
            workspace_dir=workspace_dir,
            hint_paths=hint_paths,
            hint_symbols=hint_symbols,
        )

        context_text = self._format_retrieval_context(retrieval)
        user_prompt = "\n\n".join(
            [
                f"Repository ID: {repository.id}",
                f"Repository Full Name: {repository.full_name}",
                f"Pinned Base Commit SHA: {base_commit_sha}",
                "Requirement Summary (structured JSON):",
                wrap_untrusted_data(
                    "requirement_specification",
                    requirement.model_dump_json(indent=2),
                ),
                "Retrieved Repository Context (bounded):",
                wrap_untrusted_data("retrieved_repository_context", context_text),
            ]
        )

        response = self._llm.complete_structured(
            system_prompt=self._prompt.content,
            user_prompt=user_prompt,
            response_model=RepositoryAnalysisReport,
            prompt_version=self._prompt.version,
            is_private_repo=repository.is_private,
            llm_data_sharing=repository.llm_data_sharing,
        )

        # Enforce authoritative repository identity, commit SHA, and retrieval mode
        grounded_report = response.parsed.model_copy(
            update={
                "repository_id": repository.id,
                "base_commit_sha": base_commit_sha,
                "retrieval_mode": retrieval.retrieval_mode,
            }
        )
        return RepositoryAnalysisAgentResult(
            report=grounded_report,
            retrieval=retrieval,
            usage=response.usage,
        )