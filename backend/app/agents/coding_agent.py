"""Coding Agent producing structured, hash-pinned patches validated by PatchService."""

from dataclasses import dataclass
from pathlib import Path

from app.agents.schemas import CodingOutput, ImplementationPlan, TechnicalDesign
from app.core.exceptions import ValidationFailedError
from app.execution.patch_service import PatchService
from app.execution.schemas import PatchValidationResult
from app.integrations.llm.client import LLMClient, LLMUsageMetrics
from app.intelligence.discovery import compute_sha256_bytes, is_binary_bytes
from app.prompts import load_prompt, wrap_untrusted_data

MAX_FILE_CONTEXT_CHARS = 24_000


@dataclass(frozen=True)
class CodingAgentResult:
    """Validated coding output, deterministic patch pre-validation, and LLM telemetry."""

    output: CodingOutput
    validation: PatchValidationResult
    usage: LLMUsageMetrics


class CodingAgent:
    """Generates bounded structured patches from approved plans and pre-validates them."""

    def __init__(
        self,
        llm_client: LLMClient,
        patch_service: PatchService,
        *,
        prompt_version: str = "v1",
    ) -> None:
        self._llm = llm_client
        self._patch_service = patch_service
        self._prompt = load_prompt("coding", prompt_version)

    def _read_workspace_file_snapshots(
        self,
        *,
        workspace_dir: Path,
        target_files: list[str],
    ) -> str:
        root = workspace_dir.resolve()
        snapshots: list[str] = []

        for raw_rel in target_files:
            cleaned = raw_rel.replace("\\", "/").strip().lstrip("./")
            if not cleaned or ".." in Path(cleaned).parts:
                continue
            candidate = (root / cleaned).resolve()
            if not candidate.is_relative_to(root):
                continue

            if not candidate.is_file():
                snapshots.append(
                    f"FILE: {cleaned}\nSTATUS: does_not_exist_yet (use operation='create')"
                )
                continue

            raw_bytes = candidate.read_bytes()
            if is_binary_bytes(raw_bytes[:8192]):
                snapshots.append(f"FILE: {cleaned}\nSTATUS: binary_file_not_editable")
                continue

            content_hash = compute_sha256_bytes(raw_bytes)
            text = raw_bytes.decode("utf-8")[:MAX_FILE_CONTEXT_CHARS]
            snapshots.append(
                f"FILE: {cleaned}\n"
                f"BASE_CONTENT_HASH: {content_hash}\n"
                f"CONTENT:\n{text}"
            )

        return "\n\n====================\n\n".join(snapshots)

    def run(
        self,
        *,
        workspace_dir: Path,
        base_commit_sha: str,
        design: TechnicalDesign,
        plan: ImplementationPlan,
        is_private_repo: bool,
        llm_data_sharing: bool,
        feedback: str | None = None,
        approved_protected_paths: list[str] | None = None,
    ) -> CodingAgentResult:
        """Generate a structured PatchBundle and validate it deterministically before applying."""
        file_snapshots = self._read_workspace_file_snapshots(
            workspace_dir=workspace_dir,
            target_files=plan.expected_files,
        )

        sections = [
            f"Pinned Base Commit SHA: {base_commit_sha}",
            "Approved TechnicalDesign:",
            wrap_untrusted_data("technical_design", design.model_dump_json(indent=2)),
            "Approved ImplementationPlan:",
            wrap_untrusted_data("implementation_plan", plan.model_dump_json(indent=2)),
            "Current Workspace Files and Exact SHA-256 Hashes:",
            wrap_untrusted_data("workspace_files", file_snapshots),
        ]
        if feedback:
            sections.append(
                "Deterministic Test/Review Feedback for Bounded Correction:\n"
                + wrap_untrusted_data("correction_feedback", feedback)
            )

        response = self._llm.complete_structured(
            system_prompt=self._prompt.content,
            user_prompt="\n\n".join(sections),
            response_model=CodingOutput,
            prompt_version=self._prompt.version,
            is_private_repo=is_private_repo,
            llm_data_sharing=llm_data_sharing,
        )

        if response.parsed.patch.base_commit_sha != base_commit_sha:
            raise ValidationFailedError(
                "CodingAgent returned a patch bound to an unexpected base_commit_sha.",
                details={
                    "expected_base_commit_sha": base_commit_sha,
                    "returned_base_commit_sha": response.parsed.patch.base_commit_sha,
                },
            )

        validation = self._patch_service.validate_patch(
            workspace_dir=workspace_dir,
            patch=response.parsed.patch,
            expected_files=plan.expected_files,
            declared_protected_paths=plan.declared_protected_paths,
            approved_protected_paths=approved_protected_paths,
            plan_declares_dependency_change=plan.declares_dependency_change,
        )
        return CodingAgentResult(
            output=response.parsed,
            validation=validation,
            usage=response.usage,
        )