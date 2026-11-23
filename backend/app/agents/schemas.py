"""Versioned Pydantic v2 input/output handoff contracts for vertical-slice SDLC agents."""

from typing import Literal

from pydantic import BaseModel, Field

from app.execution.schemas import PatchBundle

AGENT_SCHEMA_VERSION = "1.0"


class AcceptanceCriterion(BaseModel):
    """Testable acceptance criterion derived from the user request."""

    id: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=3, max_length=500)
    verification_method: str = Field(min_length=3, max_length=300)


class RequirementSpecification(BaseModel):
    """Structured output contract for the Requirement Analysis Agent."""

    schema_version: str = AGENT_SCHEMA_VERSION
    summary: str = Field(min_length=5, max_length=1000)
    functional_requirements: list[str] = Field(min_length=1)
    non_functional_requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)


class CodeEvidenceReference(BaseModel):
    """Traceable code provenance reference anchored to a specific commit SHA."""

    file_path: str = Field(min_length=1, max_length=512)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol_name: str = Field(min_length=1, max_length=255)
    relevance: str = Field(min_length=3, max_length=400)
    commit_sha: str = Field(min_length=7, max_length=64)


class RepositoryAnalysisReport(BaseModel):
    """Structured output contract for the Repository Analysis Agent."""

    schema_version: str = AGENT_SCHEMA_VERSION
    repository_id: str = Field(min_length=1)
    base_commit_sha: str = Field(min_length=7, max_length=64)
    retrieval_mode: Literal["hybrid", "structural_only"] = "structural_only"
    architecture_summary: str = Field(min_length=10, max_length=2000)
    relevant_files: list[str] = Field(default_factory=list)
    related_test_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[CodeEvidenceReference] = Field(default_factory=list)


class TechnicalDesign(BaseModel):
    """Architecture and design specification produced by the consolidated Planning Agent."""

    schema_version: str = AGENT_SCHEMA_VERSION
    approach_summary: str = Field(min_length=10, max_length=2000)
    affected_components: list[str] = Field(min_length=1)
    interface_changes: list[str] = Field(default_factory=list)
    reusable_components: list[str] = Field(default_factory=list)
    compatibility_notes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    dependency_changes: list[str] = Field(default_factory=list)
    protected_path_changes: list[str] = Field(default_factory=list)


class PlanTask(BaseModel):
    """Bounded implementation task with explicit files, verification, and dependencies."""

    task_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=5, max_length=1000)
    priority: Literal["high", "medium", "low"] = "medium"
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"
    dependencies: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    verification_steps: list[str] = Field(min_length=1)
    risk_notes: str = ""


class ImplementationPlan(BaseModel):
    """Ordered task breakdown and scope boundary for human plan approval."""

    schema_version: str = AGENT_SCHEMA_VERSION
    summary: str = Field(min_length=5, max_length=1000)
    tasks: list[PlanTask] = Field(min_length=1)
    expected_files: list[str] = Field(min_length=1)
    declared_protected_paths: list[str] = Field(default_factory=list)
    declares_dependency_change: bool = False


class PlanningOutput(BaseModel):
    """Combined output contract for the consolidated Architecture + Planning Agent."""

    schema_version: str = AGENT_SCHEMA_VERSION
    design: TechnicalDesign
    plan: ImplementationPlan


class CodingOutput(BaseModel):
    """Structured output contract for the Coding Agent producing a validated PatchBundle."""

    schema_version: str = AGENT_SCHEMA_VERSION
    rationale: str = Field(min_length=5, max_length=2000)
    key_decisions: list[str] = Field(default_factory=list)
    patch: PatchBundle