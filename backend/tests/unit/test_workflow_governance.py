"""Unit tests for workflow governance persistence, idempotent sync, and queued execution."""

import json
import shutil
import uuid
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
from app.core.config import Settings, load_settings
from app.execution.patch_service import PatchService
from app.execution.sandbox_client import SandboxClient
from app.execution.schemas import FilePatchOperation, PatchBundle, SearchReplaceHunk
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.llm.client import LLMClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.discovery import compute_sha256_bytes
from app.intelligence.indexer import RepositoryIndexer
from app.intelligence.retrieval import HybridRetrievalService
from app.models import (
    AgentExecutionModel,
    ApprovalModel,
    Base,
    LLMUsageRecordModel,
    ProjectModel,
    RepositoryModel,
    TaskDependencyModel,
    TaskModel,
    WorkflowRunModel,
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


def _setup_repo(db: Session, tmp_path: Path) -> tuple[RepositoryModel, Path, str, Settings]:
    workspace = tmp_path / "demo_repo"
    shutil.copytree(DEMO_FIXTURE_DIR, workspace)
    git_repo = Repo.init(str(workspace))
    try:
        with git_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Gov Tester")
            cfg.set_value("user", "email", "gov@example.com")
        git_repo.git.add(A=True)
        commit_sha = str(git_repo.index.commit("Initial commit").hexsha)
    finally:
        git_repo.close()

    project = ProjectModel(name="gov-platform", description="Governance persistence tests")
    db.add(project)
    db.flush()
    repo = RepositoryModel(
        project_id=project.id,
        name="demo-catalog-api",
        full_name="acme/demo-catalog-api",
        provider="github",
        clone_url="https://github.com/acme/demo-catalog-api.git",
        default_branch="main",
        is_private=True,
        llm_data_sharing=True,
    )
    db.add(repo)
    db.flush()

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        LLM_PROVIDER="openai",
        LLM_MODEL_DEFAULT="gpt-4o-mini",
        LLM_API_KEY="sk-test1234567890abcdefghijklmnop",  # noqa: S106
        EMBEDDING_PROVIDER="disabled",
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )
    RepositoryIndexer(
        db,
        settings=settings,
        embedding_client=EmbeddingClient(settings=settings),
        qdrant_client=QdrantIndexClient(settings=settings),
    ).index_workspace(repository=repo, workspace_dir=workspace, commit_sha=commit_sha)
    return repo, workspace, commit_sha, settings


def _make_sandbox_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            return httpx.Response(201, json={"run_id": "sbx-gov", "status": "created"})
        if request.method == "POST" and path == "/runs/sbx-gov/workspace":
            return httpx.Response(200, json={"run_id": "sbx-gov", "status": "ready"})
        if request.method == "POST" and path == "/runs/sbx-gov/execute":
            body = json.loads(request.content.decode("utf-8"))
            template = body["template"]
            return httpx.Response(
                200,
                json={
                    "execution_id": f"exec-{template}",
                    "template": template,
                    "phase": (
                        "dependency_install"
                        if template == "install_deps"
                        else "isolated_execution"
                    ),
                    "network_policy": "restricted_egress" if template == "install_deps" else "none",
                    "command_argv": ["python"],
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "duration_seconds": 0.1,
                    "timed_out": False,
                },
            )
        if request.method == "DELETE" and path == "/runs/sbx-gov":
            return httpx.Response(200, json={"run_id": "sbx-gov", "destroyed": True})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_queued_workflow_persists_governance_tables_and_handles_duplicate_delivery(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _setup_repo(db_session, tmp_path)
    readme_hash = compute_sha256_bytes((workspace / "README.md").read_bytes())

    req_json = RequirementSpecification(
        summary="Update README documentation for catalog endpoints.",
        functional_requirements=["Document limit and offset query parameters in README.md."],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                description="README mentions limit and offset.",
                verification_method="Inspect README.md",
            )
        ],
    ).model_dump_json()

    analysis_json = RepositoryAnalysisReport(
        repository_id=repo.id,
        base_commit_sha=commit_sha,
        architecture_summary="Minimal FastAPI catalog app with README docs.",
        relevant_files=["README.md"],
        related_test_files=["tests/test_items.py"],
        evidence=[
            CodeEvidenceReference(
                file_path="app/main.py",
                start_line=1,
                end_line=10,
                symbol_name="app.main",
                relevance="Entry point module.",
                commit_sha=commit_sha,
            )
        ],
    ).model_dump_json()

    plan_json = PlanningOutput(
        design=TechnicalDesign(
            approach_summary="Update README.md with pagination parameters.",
            affected_components=["README.md"],
        ),
        plan=ImplementationPlan(
            summary="Two-task plan documenting pagination.",
            tasks=[
                PlanTask(
                    task_id="T-1",
                    title="Inspect existing endpoint docs",
                    description="Verify existing endpoint descriptions in README.md.",
                    expected_files=["README.md"],
                    acceptance_criteria=["Existing docs confirmed."],
                    verification_steps=["Read README.md."],
                ),
                PlanTask(
                    task_id="T-2",
                    title="Document pagination params",
                    description="Add limit and offset descriptions to README.md.",
                    dependencies=["T-1"],
                    expected_files=["README.md"],
                    acceptance_criteria=["README documents limit/offset."],
                    verification_steps=["Verify diff."],
                ),
            ],
            expected_files=["README.md"],
        ),
    ).model_dump_json()

    coding_json = CodingOutput(
        rationale="Updated README endpoint description.",
        patch=PatchBundle(
            base_commit_sha=commit_sha,
            summary="Document pagination in README",
            operations=[
                FilePatchOperation(
                    path="README.md",
                    operation="modify",
                    base_content_hash=readme_hash,
                    hunks=[
                        SearchReplaceHunk(
                            search="- `GET /items` — Lists all catalog items along " \
                            "with the total count.",
                            replace=(
                                "- `GET /items` — Lists paginated catalog items "
                                "(`limit`, `offset`) along with the total count."
                            ),
                        )
                    ],
                )
            ],
        ),
    ).model_dump_json()

    review_json = CodeReviewReport(
        passed=True,
        summary="Documentation update is accurate.",
        findings=[],
    ).model_dump_json()

    release_json = ReleasePreparationOutput(
        pr_title="docs(items): document pagination parameters",
        pr_description="Documents pagination query parameters in README.md.",
        release_notes="Updated endpoint documentation.",
        changed_files_summary=["README.md: added pagination docs"],
        test_results_summary="Sandbox tests passed.",
        review_summary="Code review passed.",
        final_report_markdown="# Final Report\n\nDocumentation updated and verified.",
    ).model_dump_json()

    staged = iter([req_json, analysis_json, plan_json, coding_json, review_json, release_json])
    llm_call_count = 0

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal llm_call_count
        llm_call_count += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": next(staged)}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700},
            },
        )

    # Pre-create a queued workflow projection as FastAPI would before enqueuing Celery task
    queued_run = WorkflowRunModel(
        repository_id=repo.id,
        base_branch="main",
        base_commit_sha=commit_sha,
        thread_id=f"wf-{uuid.uuid4().hex}",
        idempotency_key="queued-gov-001",
        title="Document pagination",
        request_text="Update README.md for pagination.",
        status="pending",
        current_node="queued",
        cost_limit_usd=Decimal("5.0000"),
        created_by="admin",
    )
    db_session.add(queued_run)
    db_session.flush()

    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm,
        httpx.Client(transport=_make_sandbox_transport()) as mock_sbx,
    ):
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
            sandbox_client=SandboxClient(settings=settings, http_client=mock_sbx),
            checkpointer=create_memory_checkpointer(),
            settings=settings,
        )

        # Worker executes queued workflow -> pauses at plan approval
        paused = orchestrator.execute_queued_workflow(
            workflow_id=queued_run.id,
            workspace_dir=workspace,
        )
        assert paused.status == "waiting_plan_approval"
        assert paused.reused_existing is False
        assert llm_call_count == 3

        # Duplicate Celery delivery is idempotent: no extra LLM calls, no duplicate governance rows
        dup = orchestrator.execute_queued_workflow(
            workflow_id=queued_run.id,
            workspace_dir=workspace,
        )
        assert dup.reused_existing is True
        assert llm_call_count == 3

        # Approve plan -> runs coding, sandbox, review -> pauses at commit approval
        assert paused.plan_hash is not None
        commit_pause = orchestrator.resume_workflow(
            workflow_id=queued_run.id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_plan_hash=paused.plan_hash,
                approved_base_commit_sha=commit_sha,
            ),
        )
        assert commit_pause.status == "waiting_commit_approval"
        assert commit_pause.patch_hash is not None

        # Approve commit -> runs release preparation -> completes
        done = orchestrator.resume_workflow(
            workflow_id=queued_run.id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_patch_hash=commit_pause.patch_hash,
                approved_base_commit_sha=commit_sha,
            ),
        )
        assert done.status == "completed"

        # Repeated reconciliation must remain idempotent
        orchestrator.reconcile_projection(queued_run.id)

    executions = list(
        db_session.scalars(
            select(AgentExecutionModel)
            .where(AgentExecutionModel.workflow_id == queued_run.id)
            .order_by(AgentExecutionModel.step_index.asc())
        ).all()
    )
    assert [e.node_name for e in executions] == [
        "requirement_analysis",
        "repository_analysis",
        "planning",
        "human_plan_approval",
        "coding",
        "sandbox_test",
        "code_review",
        "human_commit_approval",
        "release_preparation",
    ]

    tasks = list(
        db_session.scalars(
            select(TaskModel)
            .where(TaskModel.workflow_id == queued_run.id)
            .order_by(TaskModel.task_key.asc())
        ).all()
    )
    assert [t.task_key for t in tasks] == ["T-1", "T-2"]
    assert all(t.status == "completed" for t in tasks)

    deps = list(
        db_session.scalars(
            select(TaskDependencyModel).where(TaskDependencyModel.workflow_id == queued_run.id)
        ).all()
    )
    assert len(deps) == 1
    assert deps[0].task_id == tasks[1].id
    assert deps[0].depends_on_task_id == tasks[0].id

    approvals = list(
        db_session.scalars(
            select(ApprovalModel).where(ApprovalModel.workflow_id == queued_run.id)
        ).all()
    )
    assert len(approvals) == 2
    assert {a.approval_type for a in approvals} == {"plan", "commit"}
    assert all(a.decision == "approved" and a.base_commit_sha == commit_sha for a in approvals)

    usage_rows = list(
        db_session.scalars(
            select(LLMUsageRecordModel)
            .where(LLMUsageRecordModel.workflow_id == queued_run.id)
            .order_by(LLMUsageRecordModel.call_index.asc())
        ).all()
    )
    assert len(usage_rows) == 6
    assert all(u.model == "gpt-4o-mini" and u.cost_status == "estimated" for u in usage_rows)
    assert all(u.estimated_cost_usd is not None and u.estimated_cost_usd > 0 for u in usage_rows)


def test_cancelled_queued_workflow_never_starts_in_worker(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _setup_repo(db_session, tmp_path)

    queued_run = WorkflowRunModel(
        repository_id=repo.id,
        base_branch="main",
        base_commit_sha=commit_sha,
        thread_id=f"wf-{uuid.uuid4().hex}",
        idempotency_key="queued-cancel-001",
        title="Cancelled before start",
        request_text="Should never invoke LLM.",
        status="pending",
        current_node="queued",
        cost_limit_usd=Decimal("5.0000"),
        created_by="admin",
    )
    db_session.add(queued_run)
    db_session.flush()

    llm_called = False

    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        nonlocal llm_called
        llm_called = True
        return httpx.Response(500)

    with (
        httpx.Client(transport=httpx.MockTransport(fail_if_called)) as mock_llm,
        httpx.Client(transport=_make_sandbox_transport()) as mock_sbx,
    ):
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
            sandbox_client=SandboxClient(settings=settings, http_client=mock_sbx),
            checkpointer=create_memory_checkpointer(),
            settings=settings,
        )

        cancelled = orchestrator.cancel_workflow(
            workflow_id=queued_run.id,
            actor="admin",
            reason="Aborted before worker pickup",
        )
        assert cancelled.status == "cancelled"

        worker_result = orchestrator.execute_queued_workflow(
            workflow_id=queued_run.id,
            workspace_dir=workspace,
        )
        assert worker_result.status == "cancelled"
        assert worker_result.reused_existing is True
        assert llm_called is False