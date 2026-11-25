"""Integration and sec tests for deterministic static-analysis gates and evidence persistence."""

import json
import shutil
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from app.agents.schemas import (
    AcceptanceCriterion,
    CodeEvidenceReference,
    CodeReviewReport,
    CodingOutput,
    ImplementationPlan,
    PlanningOutput,
    PlanTask,
    ReleasePreparationOutput,
    RepositoryAnalysisReport,
    RequirementSpecification,
    TechnicalDesign,
)
from app.core.config import load_settings
from app.execution.patch_service import PatchService
from app.execution.sandbox_client import SandboxClient
from app.execution.schemas import FilePatchOperation, PatchBundle, SearchReplaceHunk
from app.execution.static_analysis import PipAuditRawOutcome, StaticAnalysisService
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.llm.client import LLMClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.discovery import compute_sha256_bytes
from app.intelligence.indexer import RepositoryIndexer
from app.intelligence.retrieval import HybridRetrievalService
from app.models import (
    Base,
    CodeChangeModel,
    CodeReviewModel,
    ProjectModel,
    RepositoryModel,
    StaticAnalysisFindingModel,
    TestExecutionModel,
)
from app.workflows.checkpointer import create_memory_checkpointer
from app.workflows.graph import WorkflowOrchestrator
from app.workflows.state import HumanDecision
from cryptography.fernet import Fernet
from git import Repo
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_FIXTURE_DIR = REPO_ROOT / "fixtures" / "demo_fastapi_repo"


class _CleanPipAuditStub:
    def audit_requirements_file(self, requirements_path: Path) -> PipAuditRawOutcome:
        _ = requirements_path
        return PipAuditRawOutcome(
            available=True,
            exit_code=0,
            stdout=json.dumps({"dependencies": []}),
            stderr="",
            duration_seconds=0.04,
            tool_version="pip-audit 2.7.3",
        )


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_blocking_bandit_finding_forces_correction_even_when_llm_approves_review(
    db_session: Session,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "demo_repo"
    shutil.copytree(DEMO_FIXTURE_DIR, workspace)
    git_repo = Repo.init(str(workspace))
    try:
        with git_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Gate Tester")
            cfg.set_value("user", "email", "gate@example.com")
        git_repo.git.add(A=True)
        commit_sha = str(git_repo.index.commit("Initial commit").hexsha)
    finally:
        git_repo.close()

    project = ProjectModel(name="quality-gate-proj")
    db_session.add(project)
    db_session.flush()
    repo = RepositoryModel(
        project_id=project.id,
        name="demo-catalog-api",
        full_name="acme/demo-catalog-api",
        provider="github",
        clone_url="https://github.com/acme/demo-catalog-api.git",
        default_branch="main",
        is_private=True,
        llm_data_sharing=False,
    )
    db_session.add(repo)
    db_session.flush()

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        LLM_PROVIDER="local",
        LLM_MODEL_DEFAULT="local-coder",
        LLM_BASE_URL="http://localhost:11434/v1",
        EMBEDDING_PROVIDER="disabled",
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )
    RepositoryIndexer(
        db_session,
        settings=settings,
        embedding_client=EmbeddingClient(settings=settings),
        qdrant_client=QdrantIndexClient(settings=settings),
    ).index_workspace(repository=repo, workspace_dir=workspace, commit_sha=commit_sha)

    bandit_calls = 0

    def sandbox_handler(request: httpx.Request) -> httpx.Response:
        nonlocal bandit_calls
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            return httpx.Response(201, json={"run_id": "sbx-gate", "status": "created"})
        if request.method == "POST" and path == "/runs/sbx-gate/workspace":
            return httpx.Response(200, json={"run_id": "sbx-gate", "status": "ready"})
        if request.method == "POST" and path == "/runs/sbx-gate/execute":
            template = json.loads(request.content.decode("utf-8"))["template"]
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
                        "duration_seconds": 0.2,
                        "timed_out": False,
                    },
                )
            if template == "bandit_check":
                bandit_calls += 1
                if bandit_calls == 1:
                    # First static-analysis run detects a High Bandit B307 vulnerability
                    return httpx.Response(
                        200,
                        json={
                            "execution_id": "e-bandit-1",
                            "template": "bandit_check",
                            "phase": "isolated_execution",
                            "network_policy": "none",
                            "command_argv": ["bandit"],
                            "exit_code": 1,
                            "stdout": json.dumps(
                                {
                                    "results": [
                                        {
                                            "test_id": "B307",
                                            "issue_severity": "HIGH",
                                            "issue_confidence": "HIGH",
                                            "issue_text": "Use of insecure eval() detected.",
                                            "filename": "/workspace/app/main.py",
                                            "line_number": 20,
                                            "line_range": [20],
                                        }
                                    ]
                                }
                            ),
                            "stderr": "",
                            "duration_seconds": 0.1,
                            "timed_out": False,
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "execution_id": "e-bandit-2",
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
            stdout = "[]" if template == "ruff_check" else ""
            if template == "pytest":
                stdout = "3 passed in 0.1s"
            return httpx.Response(
                200,
                json={
                    "execution_id": f"e-{template}",
                    "template": template,
                    "phase": "isolated_execution",
                    "network_policy": "none",
                    "command_argv": [template],
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                    "duration_seconds": 0.1,
                    "timed_out": False,
                },
            )
        if request.method == "DELETE" and path == "/runs/sbx-gate":
            return httpx.Response(200, json={"run_id": "sbx-gate", "destroyed": True})
        return httpx.Response(404)

    llm_calls = 0
    captured_coding_prompts: list[str] = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        nonlocal llm_calls
        llm_calls += 1
        user_msg = json.loads(request.content.decode("utf-8"))["messages"][-1]["content"]

        if llm_calls == 1:
            content = RequirementSpecification(
                summary="Add a helper expression formatter to app/main.py.",
                functional_requirements=["Add safe catalog summary metadata to GET /health."],
                acceptance_criteria=[
                    AcceptanceCriterion(
                        id="AC-1",
                        description="GET /health stays safe and passes Bandit.",
                        verification_method="pytest + bandit",
                    )
                ],
            ).model_dump_json()
        elif llm_calls == 2:
            content = RepositoryAnalysisReport(
                repository_id=repo.id,
                base_commit_sha=commit_sha,
                architecture_summary="FastAPI service in app/main.py.",
                relevant_files=["app/main.py"],
                evidence=[
                    CodeEvidenceReference(
                        file_path="app/main.py",
                        start_line=10,
                        end_line=15,
                        symbol_name="app.main.health_check",
                        relevance="Target health endpoint.",
                        commit_sha=commit_sha,
                    )
                ],
            ).model_dump_json()
        elif llm_calls == 3:
            content = PlanningOutput(
                design=TechnicalDesign(
                    approach_summary="Return safe status payload in app/main.py.",
                    affected_components=["app/main.py"],
                ),
                plan=ImplementationPlan(
                    summary="Update health_check safely.",
                    tasks=[
                        PlanTask(
                            task_id="T-1",
                            title="Update health_check",
                            description="Update app/main.py.",
                            expected_files=["app/main.py"],
                            acceptance_criteria=["Bandit passes."],
                            verification_steps=["Run pytest and bandit."],
                        )
                    ],
                    expected_files=["app/main.py"],
                ),
            ).model_dump_json()
        elif llm_calls == 4:
            # First coding attempt introduces an unsafe eval() call
            captured_coding_prompts.append(user_msg)
            h = compute_sha256_bytes((workspace / "app/main.py").read_bytes())
            content = CodingOutput(
                rationale="Initial implementation.",
                patch=PatchBundle(
                    base_commit_sha=commit_sha,
                    summary="Initial health update",
                    operations=[
                        FilePatchOperation(
                            path="app/main.py",
                            operation="modify",
                            base_content_hash=h,
                            hunks=[
                                SearchReplaceHunk(
                                    search='    return {"status": "ok"}',
                                    replace='    return {"status": eval("\'ok\'")}',
                                )
                            ],
                        )
                    ],
                ),
            ).model_dump_json()
        elif llm_calls == 5:
            # Overly permissive LLM claims passed=True despite Bandit B307 High finding;
            # deterministic gate MUST force passed=False and trigger correction loop!
            content = CodeReviewReport(
                passed=True,
                summary="Looks fine to me.",
                findings=[],
            ).model_dump_json()
        elif llm_calls == 6:
            # Second coding attempt receives blocking Bandit B307 diagnostic and fixes it
            captured_coding_prompts.append(user_msg)
            h = compute_sha256_bytes((workspace / "app/main.py").read_bytes())
            content = CodingOutput(
                rationale="Removed unsafe eval() per Bandit B307 finding.",
                patch=PatchBundle(
                    base_commit_sha=commit_sha,
                    summary="Replace eval with literal string",
                    operations=[
                        FilePatchOperation(
                            path="app/main.py",
                            operation="modify",
                            base_content_hash=h,
                            hunks=[
                                SearchReplaceHunk(
                                    search='    return {"status": eval("\'ok\'")}',
                                    replace='    return {"status": "ok", "service": "catalog"}',
                                )
                            ],
                        )
                    ],
                ),
            ).model_dump_json()
        elif llm_calls == 7:
            content = CodeReviewReport(
                passed=True,
                summary="Unsafe eval removed; Bandit and tests pass.",
                findings=[],
            ).model_dump_json()
        else:
            content = ReleasePreparationOutput(
                pr_title="feat(health): include service label in health endpoint",
                pr_description="Includes safe static service label in GET /health.",
                release_notes="Updated health payload.",
                changed_files_summary=["app/main.py: added service label"],
                test_results_summary="All sandbox tests passed.",
                review_summary="Static analysis and review passed after fixing Bandit B307.",
                final_report_markdown="# Final Report\n\nVerified clean.",
            ).model_dump_json()

        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm,
        httpx.Client(transport=httpx.MockTransport(sandbox_handler)) as mock_sbx,
    ):
        sbx_client = SandboxClient(settings=settings, http_client=mock_sbx)
        orchestrator = WorkflowOrchestrator(
            db_session,
            llm_client=LLMClient(settings=settings, http_client=mock_llm),
            retrieval_service=HybridRetrievalService(
                db_session,
                settings=settings,
                embedding_client=EmbeddingClient(settings=settings),
                qdrant_client=QdrantIndexClient(settings=settings),
            ),
            patch_service=PatchService(settings=settings),
            sandbox_client=sbx_client,
            static_analysis_service=StaticAnalysisService(
                sandbox_client=sbx_client,
                pip_audit_runner=_CleanPipAuditStub(),
            ),
            checkpointer=create_memory_checkpointer(),
            settings=settings,
        )

        plan_pause = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Health label",
            request_text="Add safe service label to GET /health.",
            idempotency_key="wf-gate-001",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert plan_pause.plan_hash is not None

        commit_pause = orchestrator.resume_workflow(
            workflow_id=plan_pause.workflow_id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_plan_hash=plan_pause.plan_hash,
                approved_base_commit_sha=commit_sha,
            ),
        )
        assert commit_pause.status == "waiting_commit_approval"
        assert commit_pause.review_correction_loops == 1
        assert commit_pause.static_analysis_passed is True
        assert commit_pause.static_analysis_blocking_count == 0
        assert commit_pause.patch_hash is not None

        # Verify second coding prompt received the deterministic Bandit B307 finding
        assert len(captured_coding_prompts) == 2
        assert "B307" in captured_coding_prompts[1]

        completed = orchestrator.resume_workflow(
            workflow_id=plan_pause.workflow_id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_patch_hash=commit_pause.patch_hash,
                approved_base_commit_sha=commit_sha,
            ),
        )
        assert completed.status == "completed"

        # Repeated reconciliation must not duplicate persisted evidence rows
        orchestrator.reconcile_projection(plan_pause.workflow_id)

    wf_id = plan_pause.workflow_id
    changes = list(
        db_session.scalars(
            select(CodeChangeModel)
            .where(CodeChangeModel.workflow_id == wf_id)
            .order_by(CodeChangeModel.revision_index.asc())
        ).all()
    )
    test_runs = list(
        db_session.scalars(
            select(TestExecutionModel)
            .where(TestExecutionModel.workflow_id == wf_id)
            .order_by(TestExecutionModel.run_index.asc())
        ).all()
    )
    findings = list(
        db_session.scalars(
            select(StaticAnalysisFindingModel).where(
                StaticAnalysisFindingModel.workflow_id == wf_id
            )
        ).all()
    )
    reviews = list(
        db_session.scalars(
            select(CodeReviewModel)
            .where(CodeReviewModel.workflow_id == wf_id)
            .order_by(CodeReviewModel.review_index.asc())
        ).all()
    )

    assert len(changes) == 2
    assert len(test_runs) == 2
    assert len(findings) == 1
    assert findings[0].tool == "bandit"
    assert findings[0].rule_id == "B307"
    assert findings[0].severity == "High"
    assert len(reviews) == 2
    assert reviews[0].passed is False
    assert any("static analysis gate failed" in r.lower() for r in reviews[0].blocking_reasons)
    assert reviews[1].passed is True