"""End-to-end vertical-slice workflow tests on the disposable FastAPI demo repository."""

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
    CodingOutput,
    ImplementationPlan,
    PlanningOutput,
    PlanTask,
    RepositoryAnalysisReport,
    RequirementSpecification,
    TechnicalDesign,
)
from app.core.config import load_settings
from app.core.exceptions import ValidationFailedError
from app.execution.patch_service import PatchService
from app.execution.sandbox_client import SandboxClient
from app.execution.schemas import FilePatchOperation, PatchBundle, SearchReplaceHunk
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.llm.client import LLMClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.discovery import compute_sha256_bytes
from app.intelligence.indexer import RepositoryIndexer
from app.intelligence.retrieval import HybridRetrievalService
from app.models import AuditLogModel, Base, ProjectModel, RepositoryModel, WorkflowRunModel
from app.workflows.checkpointer import create_memory_checkpointer, normalize_postgres_conninfo
from app.workflows.graph import VerticalSliceOrchestrator
from app.workflows.state import HumanDecision
from cryptography.fernet import Fernet
from git import Repo
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_FIXTURE_DIR = REPO_ROOT / "fixtures" / "demo_fastapi_repo"


def _init_demo_repo(tmp_path: Path) -> tuple[Path, str]:
    dest = tmp_path / "demo_repo"
    shutil.copytree(DEMO_FIXTURE_DIR, dest)
    git_repo = Repo.init(str(dest))
    try:
        with git_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Vertical Slice Dev")
            cfg.set_value("user", "email", "dev@example.com")
        git_repo.git.add(A=True)
        sha = str(git_repo.index.commit("Initial demo catalog API").hexsha)
        return dest, sha
    finally:
        git_repo.close()


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


def _seed_and_index_demo_repo(
    db: Session,
    tmp_path: Path,
    *,
    max_llm_calls: int = 150,
) -> tuple[RepositoryModel, Path, str, object]:
    workspace, commit_sha = _init_demo_repo(tmp_path)
    project = ProjectModel(name="catalog-platform", description="Phase 4 vertical slice")
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
        llm_data_sharing=False,
    )
    db.add(repo)
    db.flush()

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        LLM_PROVIDER="local",
        LLM_MODEL_DEFAULT="local-coder",
        LLM_BASE_URL="http://localhost:11434/v1",
        EMBEDDING_PROVIDER="disabled",
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
        MAX_LLM_CALLS_PER_WORKFLOW=max_llm_calls,
    )

    indexer = RepositoryIndexer(
        db,
        settings=settings,
        embedding_client=EmbeddingClient(settings=settings),
        qdrant_client=QdrantIndexClient(settings=settings),
    )
    indexer.index_workspace(repository=repo, workspace_dir=workspace, commit_sha=commit_sha)
    return repo, workspace, commit_sha, settings


def _build_pagination_agent_responses(
    *,
    repo_id: str,
    workspace: Path,
    commit_sha: str,
    needs_clarification_first: bool = False,
) -> list[str]:
    main_hash = compute_sha256_bytes((workspace / "app/main.py").read_bytes())
    tests_hash = compute_sha256_bytes((workspace / "tests/test_items.py").read_bytes())
    readme_hash = compute_sha256_bytes((workspace / "README.md").read_bytes())

    responses: list[str] = []
    if needs_clarification_first:
        responses.append(
            RequirementSpecification(
                summary="Pagination requested for GET /items; default limit unspecified.",
                functional_requirements=["Add limit and offset pagination to GET /items."],
                ambiguities=["Should default limit remain 25 or be 20?"],
                acceptance_criteria=[
                    AcceptanceCriterion(
                        id="AC-0",
                        description="Default limit confirmed by operator.",
                        verification_method="pytest",
                    )
                ],
                needs_clarification=True,
                clarification_questions=["What default limit should GET /items use?"],
            ).model_dump_json()
        )

    responses.append(
        RequirementSpecification(
            summary=(
                "Add validated limit and offset query parameters to GET /items "
                "with unit tests and README documentation."
            ),
            functional_requirements=[
                "Accept limit (default 25, 1..100) and offset (default 0, >=0) on GET /items.",
                "Preserve total item count in ItemListResponse.",
                "Add pagination tests and document parameters in README.md.",
            ],
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="AC-1",
                    description="GET /items?limit=5&offset=5 returns 5 items starting at item-006.",
                    verification_method="pytest tests/test_items.py",
                ),
                AcceptanceCriterion(
                    id="AC-2",
                    description="Invalid limit=0 or offset=-1 returns HTTP 422.",
                    verification_method="pytest tests/test_items.py",
                ),
            ],
        ).model_dump_json()
    )

    responses.append(
        RepositoryAnalysisReport(
            repository_id=repo_id,
            base_commit_sha=commit_sha,
            architecture_summary="FastAPI catalog service in app/main.py backed by app/store.py.",
            relevant_files=["app/main.py", "app/schemas.py", "README.md"],
            related_test_files=["tests/test_items.py"],
            dependencies=["fastapi", "pydantic"],
            key_findings=["GET /items returns all 25 items without slicing."],
            evidence=[
                CodeEvidenceReference(
                    file_path="app/main.py",
                    start_line=17,
                    end_line=21,
                    symbol_name="app.main.list_items",
                    relevance="Target list endpoint to add limit/offset pagination.",
                    commit_sha=commit_sha,
                )
            ],
        ).model_dump_json()
    )

    responses.append(
        PlanningOutput(
            design=TechnicalDesign(
                approach_summary=(
                    "Add FastAPI Query-validated limit (default 25, ge=1, le=100) and "
                    "offset (default 0, ge=0) parameters to list_items while preserving total."
                ),
                affected_components=["app/main.py", "tests/test_items.py", "README.md"],
                interface_changes=["GET /items accepts optional limit and offset query params."],
                compatibility_notes=[
                    "Default limit=25 preserves existing GET /items response for baseline callers."
                ],
            ),
            plan=ImplementationPlan(
                summary="Implement validated pagination on GET /items, add tests, update README.",
                tasks=[
                    PlanTask(
                        task_id="T-1",
                        title="Add limit/offset pagination to GET /items",
                        description="Validate and slice items in app/main.py.",
                        expected_files=["app/main.py"],
                        acceptance_criteria=["GET /items slices accurately and validates bounds."],
                        verification_steps=["Run pytest in isolated sandbox."],
                    ),
                    PlanTask(
                        task_id="T-2",
                        title="Add pagination tests and update README",
                        description="Extend tests/test_items.py and document params in README.md.",
                        dependencies=["T-1"],
                        expected_files=["tests/test_items.py", "README.md"],
                        acceptance_criteria=["All baseline and pagination tests pass."],
                        verification_steps=["Run pytest in isolated sandbox."],
                    ),
                ],
                expected_files=["app/main.py", "tests/test_items.py", "README.md"],
            ),
        ).model_dump_json()
    )

    responses.append(
        CodingOutput(
            rationale="Implemented validated limit/offset pagination, tests, and README update.",
            key_decisions=["Used default limit=25 to keep baseline test backwards-compatible."],
            patch=PatchBundle(
                base_commit_sha=commit_sha,
                summary="Add limit/offset pagination to GET /items with tests and docs",
                operations=[
                    FilePatchOperation(
                        path="app/main.py",
                        operation="modify",
                        base_content_hash=main_hash,
                        hunks=[
                            SearchReplaceHunk(
                                search="from fastapi import FastAPI, HTTPException, status",
                                replace="from fastapi import FastAPI, HTTPException, Query, status",
                            ),
                            SearchReplaceHunk(
                                search=(
                                    "def list_items() -> ItemListResponse:\n"
                                    '    """Return all catalog items."""\n'
                                    "    items = list_all_items()\n"
                                    "    return ItemListResponse(items=items, total=len(items))"
                                ),
                                replace=(
                                    "def list_items(\n"
                                    "    limit: int = Query(default=25, ge=1, le=100),\n"
                                    "    offset: int = Query(default=0, ge=0),\n"
                                    ") -> ItemListResponse:\n"
                                    '    """Return paginated catalog items."""\n'
                                    "    all_items = list_all_items()\n"
                                    "    return ItemListResponse(\n"
                                    "        items=all_items[offset : offset + limit],\n"
                                    "        total=len(all_items),\n"
                                    "    )"
                                ),
                            ),
                        ],
                    ),
                    FilePatchOperation(
                        path="tests/test_items.py",
                        operation="modify",
                        base_content_hash=tests_hash,
                        hunks=[
                            SearchReplaceHunk(
                                search=(
                                    '    missing = client.get("/items/item-999")\n'
                                    "    assert missing.status_code == 404"
                                ),
                                replace=(
                                    '    missing = client.get("/items/item-999")\n'
                                    "    assert missing.status_code == 404\n\n\n"
                                    "def test_list_items_pagination_and_validation() -> None:\n"
                                    '    page = client.get("/items?limit=5&offset=5")\n'
                                    "    assert page.status_code == 200\n"
                                    "    body = page.json()\n"
                                    '    assert body["total"] == 25\n'
                                    '    assert len(body["items"]) == 5\n'
                                    '    assert body["items"][0]["id"] == "item-006"\n'
                                    '    assert client.get("/items?limit=0").status_code == 422\n'
                                    '    assert client.get("/items?offset=-1").status_code == 422'
                                ),
                            )
                        ],
                    ),
                    FilePatchOperation(
                        path="README.md",
                        operation="modify",
                        base_content_hash=readme_hash,
                        hunks=[
                            SearchReplaceHunk(
                                search=(
                                    "- `GET /items` — Lists all catalog items "
                                    "along with the total count."
                                ),
                                replace=(
                                    "- `GET /items` — Lists paginated catalog items "
                                    "(`limit` 1..100, `offset` >= 0) along with the total count."
                                ),
                            )
                        ],
                    ),
                ],
            ),
        ).model_dump_json()
    )
    return responses


def _make_sandbox_mock_handler(
    events: list[str],
    *,
    pytest_exit_code: int = 0,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            events.append("sandbox:create")
            return httpx.Response(201, json={"run_id": "sbx-run-1", "status": "created"})
        if request.method == "POST" and path == "/runs/sbx-run-1/workspace":
            events.append("sandbox:upload")
            return httpx.Response(
                200,
                json={"run_id": "sbx-run-1", "status": "ready", "files_extracted": 7},
            )
        if request.method == "POST" and path == "/runs/sbx-run-1/execute":
            body = json.loads(request.content.decode("utf-8"))
            template = body["template"]
            events.append(f"sandbox:exec:{template}")
            if template == "install_deps":
                return httpx.Response(
                    200,
                    json={
                        "execution_id": "exec-install",
                        "template": "install_deps",
                        "phase": "dependency_install",
                        "network_policy": "restricted_egress",
                        "command_argv": ["python", "-c", "..."],
                        "exit_code": 0,
                        "stdout": "deps installed",
                        "stderr": "",
                        "duration_seconds": 0.5,
                        "timed_out": False,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "execution_id": "exec-pytest",
                    "template": "pytest",
                    "phase": "isolated_execution",
                    "network_policy": "none",
                    "command_argv": ["/sandbox_venv/bin/python", "-m", "pytest", "-q", "tests"],
                    "exit_code": pytest_exit_code,
                    "stdout": "4 passed in 0.25s" if pytest_exit_code == 0 else "1 failed",
                    "stderr": "",
                    "duration_seconds": 0.25,
                    "timed_out": False,
                },
            )
        if request.method == "DELETE" and path == "/runs/sbx-run-1":
            events.append("sandbox:destroy")
            return httpx.Response(200, json={"run_id": "sbx-run-1", "destroyed": True})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_normalize_postgres_conninfo() -> None:
    assert (
        normalize_postgres_conninfo("postgresql+psycopg://postgres:pass@db:5432/ai_sdlc")
        == "postgresql://postgres:pass@db:5432/ai_sdlc"
    )
    assert (
        normalize_postgres_conninfo("postgresql://postgres:pass@db:5432/ai_sdlc")
        == "postgresql://postgres:pass@db:5432/ai_sdlc"
    )


def test_vertical_slice_end_to_end_pagination_with_approval_and_sandbox(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(db_session, tmp_path)
    original_main_hash = compute_sha256_bytes((workspace / "app/main.py").read_bytes())

    staged = iter(
        _build_pagination_agent_responses(
            repo_id=repo.id,
            workspace=workspace,
            commit_sha=commit_sha,
        )
    )
    llm_calls = 0

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal llm_calls
        llm_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": next(staged)}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
            },
        )

    sandbox_events: list[str] = []
    checkpointer = create_memory_checkpointer()

    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
        httpx.Client(transport=_make_sandbox_mock_handler(sandbox_events)) as mock_sbx_http,
    ):
        orchestrator = VerticalSliceOrchestrator(
            db_session,
            llm_client=LLMClient(settings=settings, http_client=mock_llm_http),  # type: ignore[arg-type]
            retrieval_service=HybridRetrievalService(
                db_session,
                settings=settings,  # type: ignore[arg-type]
                embedding_client=EmbeddingClient(settings=settings),  # type: ignore[arg-type]
                qdrant_client=QdrantIndexClient(settings=settings),  # type: ignore[arg-type]
            ),
            patch_service=PatchService(settings=settings),  # type: ignore[arg-type]
            sandbox_client=SandboxClient(settings=settings, http_client=mock_sbx_http),  # type: ignore[arg-type]
            checkpointer=checkpointer,
            settings=settings,  # type: ignore[arg-type]
        )

        # Step 1: Start workflow -> runs analyze + plan, then pauses for human plan approval
        paused = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Add pagination to GET /items",
            request_text="Add validated limit/offset pagination to GET /items with tests and docs.",
            idempotency_key="wf-pagination-001",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert paused.status == "waiting_plan_approval"
        assert paused.interrupted is True
        assert paused.plan_hash is not None
        assert paused.interrupt_payload is not None
        assert paused.interrupt_payload["type"] == "plan_approval_required"
        assert paused.llm_calls_count == 3
        assert llm_calls == 3

        # Workspace must NOT be modified before human plan approval
        assert compute_sha256_bytes((workspace / "app/main.py").read_bytes()) == original_main_hash
        assert sandbox_events == []

        # Idempotent re-submission returns existing snapshot without extra LLM calls
        duplicate = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Add pagination to GET /items",
            request_text="Duplicate submission",
            idempotency_key="wf-pagination-001",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert duplicate.reused_existing is True
        assert duplicate.workflow_id == paused.workflow_id
        assert llm_calls == 3

        # Simulate projection drift in WorkflowRunModel and verify reconciliation repairs it
        run_row = db_session.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == paused.workflow_id)
        )
        assert run_row is not None
        run_row.status = "running"
        db_session.flush()
        reconciled = orchestrator.reconcile_projection(paused.workflow_id)
        assert reconciled.status == "waiting_plan_approval"
        assert run_row.status == "waiting_plan_approval"

        # Step 2: Resume with explicit human approval bound to plan_hash and base_commit_sha
        completed = orchestrator.resume_workflow(
            workflow_id=paused.workflow_id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_plan_hash=paused.plan_hash,
                approved_base_commit_sha=commit_sha,
            ),
        )

    assert completed.status == "completed"
    assert completed.interrupted is False
    assert completed.sandbox_passed is True
    assert completed.patch_hash is not None
    assert completed.llm_calls_count == 4
    assert completed.node_history == [
        "requirement_analysis",
        "repository_analysis",
        "planning",
        "human_plan_approval",
        "coding",
        "sandbox_test",
    ]
    assert sandbox_events == [
        "sandbox:create",
        "sandbox:upload",
        "sandbox:exec:install_deps",
        "sandbox:exec:pytest",
        "sandbox:destroy",
    ]
    assert "offset : offset + limit" in (workspace / "app/main.py").read_text(encoding="utf-8")
    assert "test_list_items_pagination_and_validation" in (
        workspace / "tests/test_items.py"
    ).read_text(encoding="utf-8")
    assert "(`limit` 1..100, `offset` >= 0)" in (workspace / "README.md").read_text(
        encoding="utf-8"
    )

    actions = list(db_session.scalars(select(AuditLogModel.action)).all())
    assert "workflow.start" in actions
    assert "workflow.resume.approve" in actions


def test_clarification_loop_and_plan_rejection_leave_workspace_untouched(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(db_session, tmp_path)
    original_main_hash = compute_sha256_bytes((workspace / "app/main.py").read_bytes())

    staged = iter(
        _build_pagination_agent_responses(
            repo_id=repo.id,
            workspace=workspace,
            commit_sha=commit_sha,
            needs_clarification_first=True,
        )
    )

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(staged)}}]},
        )

    sandbox_events: list[str] = []
    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
        httpx.Client(transport=_make_sandbox_mock_handler(sandbox_events)) as mock_sbx_http,
    ):
        orchestrator = VerticalSliceOrchestrator(
            db_session,
            llm_client=LLMClient(settings=settings, http_client=mock_llm_http),  # type: ignore[arg-type]
            retrieval_service=HybridRetrievalService(
                db_session,
                settings=settings,  # type: ignore[arg-type]
                embedding_client=EmbeddingClient(settings=settings),  # type: ignore[arg-type]
                qdrant_client=QdrantIndexClient(settings=settings),  # type: ignore[arg-type]
            ),
            patch_service=PatchService(settings=settings),  # type: ignore[arg-type]
            sandbox_client=SandboxClient(settings=settings, http_client=mock_sbx_http),  # type: ignore[arg-type]
            checkpointer=create_memory_checkpointer(),
            settings=settings,  # type: ignore[arg-type]
        )

        # Pauses for clarification first
        clarify_pause = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Add pagination",
            request_text="Add pagination to GET /items.",
            idempotency_key="wf-clarify-001",
            cost_limit_usd=Decimal("3.0000"),
            actor="admin",
        )
        assert clarify_pause.status == "waiting_clarification"
        assert clarify_pause.interrupt_payload is not None
        assert clarify_pause.interrupt_payload["type"] == "clarification_required"

        # Operator supplies clarification -> advances to human_plan_approval
        plan_pause = orchestrator.resume_workflow(
            workflow_id=clarify_pause.workflow_id,
            decision=HumanDecision(
                action="clarify",
                actor="admin",
                clarification_answers="Use default limit=25 to stay backwards-compatible.",
            ),
        )
        assert plan_pause.status == "waiting_plan_approval"

        # Operator rejects plan -> terminates cleanly without coding or sandbox runs
        rejected = orchestrator.resume_workflow(
            workflow_id=clarify_pause.workflow_id,
            decision=HumanDecision(
                action="reject",
                actor="admin",
                reason="Scope postponed to next sprint.",
            ),
        )

    assert rejected.status == "rejected"
    assert rejected.interrupted is False
    assert sandbox_events == []
    assert compute_sha256_bytes((workspace / "app/main.py").read_bytes()) == original_main_hash


def test_workflow_safety_gates_cost_limit_approval_binding_and_budget_exhaustion(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(
        db_session,
        tmp_path,
        max_llm_calls=1,
    )

    staged = iter(
        _build_pagination_agent_responses(
            repo_id=repo.id,
            workspace=workspace,
            commit_sha=commit_sha,
        )
    )

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(staged)}}]},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
        httpx.Client(transport=_make_sandbox_mock_handler([])) as mock_sbx_http,
    ):
        orchestrator = VerticalSliceOrchestrator(
            db_session,
            llm_client=LLMClient(settings=settings, http_client=mock_llm_http),  # type: ignore[arg-type]
            retrieval_service=HybridRetrievalService(
                db_session,
                settings=settings,  # type: ignore[arg-type]
                embedding_client=EmbeddingClient(settings=settings),  # type: ignore[arg-type]
                qdrant_client=QdrantIndexClient(settings=settings),  # type: ignore[arg-type]
            ),
            patch_service=PatchService(settings=settings),  # type: ignore[arg-type]
            sandbox_client=SandboxClient(settings=settings, http_client=mock_sbx_http),  # type: ignore[arg-type]
            checkpointer=create_memory_checkpointer(),
            settings=settings,  # type: ignore[arg-type]
        )

        # 1. Refuses to start with non-positive cost_limit_usd
        with pytest.raises(ValidationFailedError):
            orchestrator.start_workflow(
                repository=repo,
                base_branch="main",
                base_commit_sha=commit_sha,
                workspace_dir=workspace,
                title="Missing cost limit",
                request_text="Add pagination.",
                idempotency_key="wf-zero-budget",
                cost_limit_usd=Decimal("0"),
                actor="admin",
            )

        # 2. Enforces MAX_LLM_CALLS_PER_WORKFLOW=1 cleanly
        exhausted = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Budget cap test",
            request_text="Add pagination.",
            idempotency_key="wf-budget-cap",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert exhausted.status == "failed"
        assert exhausted.error_code == "BUDGET_EXHAUSTED"
        assert exhausted.llm_calls_count == 1


def test_approval_binding_mismatch_and_incompatible_schema_version_are_blocked(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(db_session, tmp_path)
    staged = iter(
        _build_pagination_agent_responses(
            repo_id=repo.id,
            workspace=workspace,
            commit_sha=commit_sha,
        )
    )

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(staged)}}]},
        )

    checkpointer = create_memory_checkpointer()
    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
        httpx.Client(transport=_make_sandbox_mock_handler([])) as mock_sbx_http,
    ):
        orchestrator = VerticalSliceOrchestrator(
            db_session,
            llm_client=LLMClient(settings=settings, http_client=mock_llm_http),  # type: ignore[arg-type]
            retrieval_service=HybridRetrievalService(
                db_session,
                settings=settings,  # type: ignore[arg-type]
                embedding_client=EmbeddingClient(settings=settings),  # type: ignore[arg-type]
                qdrant_client=QdrantIndexClient(settings=settings),  # type: ignore[arg-type]
            ),
            patch_service=PatchService(settings=settings),  # type: ignore[arg-type]
            sandbox_client=SandboxClient(settings=settings, http_client=mock_sbx_http),  # type: ignore[arg-type]
            checkpointer=checkpointer,
            settings=settings,  # type: ignore[arg-type]
        )

        paused = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Approval binding check",
            request_text="Add pagination.",
            idempotency_key="wf-mismatch-001",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert paused.status == "waiting_plan_approval"

        # Tamper with checkpoint state_schema_version to verify resume refuses incompatible schema
        config = {"configurable": {"thread_id": paused.thread_id}}
        orchestrator._graph.update_state(config, {"state_schema_version": "0.9-legacy"})
        with pytest.raises(ValidationFailedError):
            orchestrator.resume_workflow(
                workflow_id=paused.workflow_id,
                decision=HumanDecision(
                    action="approve",
                    actor="admin",
                    approved_plan_hash=paused.plan_hash,
                    approved_base_commit_sha=commit_sha,
                ),
            )

        # Restore compatible schema version, then test mismatched plan_hash rejection
        orchestrator._graph.update_state(config, {"state_schema_version": "1.0"})
        mismatch = orchestrator.resume_workflow(
            workflow_id=paused.workflow_id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_plan_hash="0" * 64,
                approved_base_commit_sha=commit_sha,
            ),
        )
        assert mismatch.status == "failed"
        assert mismatch.error_code == "APPROVAL_BINDING_MISMATCH"
        assert "coding" not in mismatch.node_history