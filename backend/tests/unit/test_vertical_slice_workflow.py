"""Comprehensive tests for the deterministic seven-agent SDLC LangGraph orchestrator."""

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
    CodeReviewFinding,
    CodeReviewReport,
    CodingOutput,
    ImplementationPlan,
    PlanningOutput,
    PlanTask,
    ReleasePreparationOutput,
    RepositoryAnalysisReport,
    RequirementSpecification,
    TechnicalDesign,
    TestFailureDiagnosis,
)
from app.core.config import Settings, load_settings
from app.core.exceptions import ConflictError, ValidationFailedError
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
from app.models import AuditLogModel, Base, ProjectModel, RepositoryModel, WorkflowRunModel
from app.observability.tracing import build_safe_trace_metadata, should_enable_langsmith_tracing
from app.workflows.checkpointer import create_memory_checkpointer, normalize_postgres_conninfo
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
            duration_seconds=0.05,
            tool_version="pip-audit 2.7.3",
        )


def _init_demo_repo(tmp_path: Path) -> tuple[Path, str]:
    dest = tmp_path / "demo_repo"
    shutil.copytree(DEMO_FIXTURE_DIR, dest)
    git_repo = Repo.init(str(dest))
    try:
        with git_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "SDLC Dev")
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
    is_private: bool = True,
    llm_data_sharing: bool = False,
    max_llm_calls: int = 150,
    max_correction_per_failure: int = 3,
    max_correction_per_workflow: int = 5,
    langsmith_tracing: bool = False,
) -> tuple[RepositoryModel, Path, str, Settings]:
    workspace, commit_sha = _init_demo_repo(tmp_path)
    project = ProjectModel(name="catalog-platform", description="SDLC workflow tests")
    db.add(project)
    db.flush()

    repo = RepositoryModel(
        project_id=project.id,
        name="demo-catalog-api",
        full_name="acme/demo-catalog-api",
        provider="github",
        clone_url="https://github.com/acme/demo-catalog-api.git",
        default_branch="main",
        is_private=is_private,
        llm_data_sharing=llm_data_sharing,
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
        MAX_CORRECTION_LOOPS_PER_FAILURE=max_correction_per_failure,
        MAX_CORRECTION_LOOPS_PER_WORKFLOW=max_correction_per_workflow,
        LANGSMITH_TRACING=langsmith_tracing,
        LANGSMITH_API_KEY="lsv2_test_key_1234567890abcdef" if langsmith_tracing else "",  # noqa: S106
    )

    indexer = RepositoryIndexer(
        db,
        settings=settings,
        embedding_client=EmbeddingClient(settings=settings),
        qdrant_client=QdrantIndexClient(settings=settings),
    )
    indexer.index_workspace(repository=repo, workspace_dir=workspace, commit_sha=commit_sha)
    return repo, workspace, commit_sha, settings


def _make_req_json(*, needs_clarification: bool = False) -> str:
    if needs_clarification:
        return RequirementSpecification(
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

    return RequirementSpecification(
        summary="Add validated limit and offset query parameters to " \
        "GET /items with tests and docs.",
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
        ],
    ).model_dump_json()


def _make_analysis_json(repo_id: str, commit_sha: str) -> str:
    return RepositoryAnalysisReport(
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


def _make_planning_json() -> str:
    return PlanningOutput(
        design=TechnicalDesign(
            approach_summary=(
                "Add FastAPI Query-validated limit (default 25, ge=1, le=100) and "
                "offset (default 0, ge=0) parameters to list_items while preserving total."
            ),
            affected_components=["app/main.py", "tests/test_items.py", "README.md"],
            interface_changes=["GET /items accepts optional limit and offset query params."],
            compatibility_notes=["Default limit=25 preserves baseline GET /items behavior."],
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


def _make_initial_coding_json(
    workspace: Path,
    commit_sha: str,
    *,
    default_limit: int = 25,
) -> str:
    main_hash = compute_sha256_bytes((workspace / "app/main.py").read_bytes())
    tests_hash = compute_sha256_bytes((workspace / "tests/test_items.py").read_bytes())
    readme_hash = compute_sha256_bytes((workspace / "README.md").read_bytes())
    return CodingOutput(
        rationale=f"Implemented limit (default {default_limit}) and offset pagination.",
        key_decisions=[f"Set default limit={default_limit}."],
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
                                f"    limit: int = Query(default={default_limit}, ge=1, le=100),\n"
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


def _make_clean_review_json() -> str:
    return CodeReviewReport(
        passed=True,
        summary="Clean pagination implementation matching all acceptance criteria.",
        requirement_coverage_notes=["Validated limit/offset slicing and total preserved."],
        test_quality_notes=["Baseline and pagination boundary tests pass."],
        findings=[],
    ).model_dump_json()


def _make_failing_review_json() -> str:
    return CodeReviewReport(
        passed=False,
        summary="Default limit broke backwards compatibility.",
        findings=[
            CodeReviewFinding(
                id="CR-1",
                severity="High",
                classification="confirmed_defect",
                category="compatibility",
                file_path="app/main.py",
                title="Incorrect default limit",
                description="Default limit should be 25 to preserve baseline responses.",
                recommendation="Update default limit to 25.",
            )
        ],
        blocking_reasons=["High confirmed compatibility defect in app/main.py."],
    ).model_dump_json()


def _make_release_json() -> str:
    return ReleasePreparationOutput(
        pr_title="feat(items): add validated limit/offset pagination to GET /items",
        pr_description="Adds validated `limit` and `offset` query params to `GET /items`.",
        release_notes="Added pagination support to catalog listing endpoint.",
        changed_files_summary=[
            "app/main.py: added Query-validated limit/offset slicing",
            "tests/test_items.py: added pagination and validation tests",
            "README.md: documented pagination query parameters",
        ],
        test_results_summary="Isolated sandbox pytest passed (4 passed).",
        review_summary="Code review passed with 0 blocking defects.",
        limitations=["In-memory catalog store only."],
        final_report_markdown="# Completion Report\n\nVerified pagination implementation.",
    ).model_dump_json()


def _make_sandbox_mock_handler(
    events: list[str],
    *,
    pytest_exit_codes: list[int] | None = None,
) -> httpx.MockTransport:
    exit_queue = list(pytest_exit_codes or [0])

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
                        "duration_seconds": 0.4,
                        "timed_out": False,
                    },
                )
            if template in {"ruff_check", "mypy_check", "bandit_check"}:
                stdout = "[]" if template == "ruff_check" else ""
                if template == "bandit_check":
                    stdout = json.dumps({"results": []})
                return httpx.Response(
                    200,
                    json={
                        "execution_id": f"exec-{template}",
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
            code = exit_queue.pop(0) if len(exit_queue) > 1 else exit_queue[0]
            return httpx.Response(
                200,
                json={
                    "execution_id": "exec-pytest",
                    "template": "pytest",
                    "phase": "isolated_execution",
                    "network_policy": "none",
                    "command_argv": ["/sandbox_venv/bin/python", "-m", "pytest", "-q", "tests"],
                    "exit_code": code,
                    "stdout": "4 passed in 0.25s" if code == 0 else "FAILED test_list_items",
                    "stderr": "" if code == 0 else "AssertionError: assert 20 == 25",
                    "duration_seconds": 0.25,
                    "timed_out": False,
                },
            )
        if request.method == "DELETE" and path == "/runs/sbx-run-1":
            events.append("sandbox:destroy")
            return httpx.Response(200, json={"run_id": "sbx-run-1", "destroyed": True})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _make_orchestrator(
    db: Session,
    settings: Settings,
    llm_http: httpx.Client,
    sbx_http: httpx.Client,
) -> WorkflowOrchestrator:
    sbx_client = SandboxClient(settings=settings, http_client=sbx_http)
    return WorkflowOrchestrator(
        db,
        llm_client=LLMClient(settings=settings, http_client=llm_http),
        retrieval_service=HybridRetrievalService(
            db,
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


def test_normalize_postgres_conninfo_and_langsmith_privacy_policy() -> None:
    assert (
        normalize_postgres_conninfo("postgresql+psycopg://postgres:pass@db:5432/ai_sdlc")
        == "postgresql://postgres:pass@db:5432/ai_sdlc"
    )
    fernet_key = Fernet.generate_key().decode("utf-8")
    traced_settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        LANGSMITH_TRACING=True,
        LANGSMITH_API_KEY="lsv2_test_key_1234567890abcdef",  # noqa: S106
    )
    assert (
        should_enable_langsmith_tracing(
            settings=traced_settings,
            is_private_repo=True,
            llm_data_sharing=False,
        )
        is False
    )
    assert (
        should_enable_langsmith_tracing(
            settings=traced_settings,
            is_private_repo=True,
            llm_data_sharing=True,
        )
        is True
    )
    meta = build_safe_trace_metadata(
        workflow_id="wf-1",
        repository_id="repo-1",
        base_commit_sha="a" * 40,
        extra={"api_key": "sk-secret1234567890abcdefghij", "note": "ok"},
    )
    assert meta["api_key"] == "[REDACTED]"
    assert meta["note"] == "ok"


def test_full_seven_agent_workflow_with_test_correction_loop_and_dual_approvals(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(
        db_session,
        tmp_path,
        is_private=True,
        llm_data_sharing=True,
        langsmith_tracing=True,
    )
    original_main_hash = compute_sha256_bytes((workspace / "app/main.py").read_bytes())

    call_idx = 0

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            content = _make_req_json()
        elif call_idx == 2:
            content = _make_analysis_json(repo.id, commit_sha)
        elif call_idx == 3:
            content = _make_planning_json()
        elif call_idx == 4:
            content = _make_initial_coding_json(workspace, commit_sha, default_limit=20)
        elif call_idx == 5:
            content = TestFailureDiagnosis(
                summary="Default limit=20 broke baseline test expecting 25 items.",
                failure_category="assertion_failure",
                root_cause="list_items defaulted limit to 20 instead of 25.",
                failing_tests=["tests/test_items.py::test_list_items_returns_all_seeded_items"],
                affected_files=["app/main.py"],
                recommended_fixes=["Change default=20 to default=25 in app/main.py."],
                correction_instructions="Update Query(default=20) to Query(default=25).",
                is_fixable_by_coding_agent=True,
            ).model_dump_json()
        elif call_idx == 6:
            current_main_hash = compute_sha256_bytes((workspace / "app/main.py").read_bytes())
            content = CodingOutput(
                rationale="Fixed default limit from 20 to 25 per test failure diagnosis.",
                key_decisions=["Preserved backwards compatibility with baseline test."],
                patch=PatchBundle(
                    base_commit_sha=commit_sha,
                    summary="Fix default pagination limit to 25",
                    operations=[
                        FilePatchOperation(
                            path="app/main.py",
                            operation="modify",
                            base_content_hash=current_main_hash,
                            hunks=[
                                SearchReplaceHunk(
                                    search="limit: int = Query(default=20, ge=1, le=100),",
                                    replace="limit: int = Query(default=25, ge=1, le=100),",
                                )
                            ],
                        )
                    ],
                ),
            ).model_dump_json()
        elif call_idx == 7:
            content = _make_clean_review_json()
        else:
            content = _make_release_json()

        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
            },
        )

    sandbox_events: list[str] = []
    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
        httpx.Client(
            transport=_make_sandbox_mock_handler(sandbox_events, pytest_exit_codes=[1, 0])
        ) as mock_sbx_http,
    ):
        orchestrator = _make_orchestrator(db_session, settings, mock_llm_http, mock_sbx_http)

        plan_pause = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Add pagination to GET /items",
            request_text="Add validated limit/offset pagination to GET /items with tests and docs.",
            idempotency_key="wf-full-001",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert plan_pause.status == "waiting_plan_approval"
        assert plan_pause.tracing_enabled is True
        assert plan_pause.plan_hash is not None
        assert compute_sha256_bytes((workspace / "app/main.py").read_bytes()) == original_main_hash

        dup = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Add pagination",
            request_text="Duplicate",
            idempotency_key="wf-full-001",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert dup.reused_existing is True
        assert call_idx == 3

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
        assert commit_pause.interrupted is True
        assert commit_pause.test_correction_loops == 1
        assert commit_pause.total_correction_loops == 1
        assert commit_pause.sandbox_passed is True
        assert commit_pause.static_analysis_passed is True
        assert commit_pause.review_passed is True
        assert commit_pause.patch_hash is not None
        assert commit_pause.interrupt_payload is not None
        assert commit_pause.interrupt_payload["type"] == "commit_approval_required"
        assert commit_pause.interrupt_payload["static_analysis_report"]["passed"] is True
        assert set(commit_pause.interrupt_payload["files_changed"]) == {
            "README.md",
            "app/main.py",
            "tests/test_items.py",
        }
        assert "default=25" in commit_pause.interrupt_payload["raw_diff"]
        assert "default=20" not in commit_pause.interrupt_payload["raw_diff"]

        run_row = db_session.scalar(
            select(WorkflowRunModel).where(WorkflowRunModel.id == plan_pause.workflow_id)
        )
        assert run_row is not None
        run_row.status = "running"
        db_session.flush()
        reconciled = orchestrator.reconcile_projection(plan_pause.workflow_id)
        assert reconciled.status == "waiting_commit_approval"

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
    assert completed.interrupted is False
    assert completed.commit_approved is True
    assert completed.pr_title is not None and completed.pr_title.startswith("feat(items):")
    assert completed.node_history == [
        "requirement_analysis",
        "repository_analysis",
        "planning",
        "human_plan_approval",
        "coding",
        "sandbox_test",
        "test_failure_analysis",
        "coding",
        "sandbox_test",
        "static_analysis",
        "code_review",
        "human_commit_approval",
        "release_preparation",
    ]
    actions = list(db_session.scalars(select(AuditLogModel.action)).all())
    assert "workflow.start" in actions
    assert actions.count("workflow.resume.approve") == 2


def test_review_correction_loop_and_correction_cap_prevent_infinite_loops(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(
        db_session,
        tmp_path,
        max_correction_per_failure=1,
        max_correction_per_workflow=2,
    )

    call_idx = 0

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            content = _make_req_json()
        elif call_idx == 2:
            content = _make_analysis_json(repo.id, commit_sha)
        elif call_idx == 3:
            content = _make_planning_json()
        elif call_idx == 4:
            content = _make_initial_coding_json(workspace, commit_sha, default_limit=20)
        elif call_idx == 5:
            content = _make_failing_review_json()
        elif call_idx == 6:
            current_main_hash = compute_sha256_bytes((workspace / "app/main.py").read_bytes())
            content = CodingOutput(
                rationale="Attempted fix for review finding.",
                patch=PatchBundle(
                    base_commit_sha=commit_sha,
                    summary="Update default limit",
                    operations=[
                        FilePatchOperation(
                            path="app/main.py",
                            operation="modify",
                            base_content_hash=current_main_hash,
                            hunks=[
                                SearchReplaceHunk(
                                    search="limit: int = Query(default=20, ge=1, le=100),",
                                    replace="limit: int = Query(default=25, ge=1, le=100),",
                                )
                            ],
                        )
                    ],
                ),
            ).model_dump_json()
        else:
            content = _make_failing_review_json()

        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
        httpx.Client(transport=_make_sandbox_mock_handler([])) as mock_sbx_http,
    ):
        orchestrator = _make_orchestrator(db_session, settings, mock_llm_http, mock_sbx_http)
        plan_pause = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Review cap check",
            request_text="Add pagination.",
            idempotency_key="wf-review-cap",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert plan_pause.plan_hash is not None
        halted = orchestrator.resume_workflow(
            workflow_id=plan_pause.workflow_id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_plan_hash=plan_pause.plan_hash,
                approved_base_commit_sha=commit_sha,
            ),
        )

    assert halted.status == "failed"
    assert halted.error_code == "CORRECTION_LIMIT_EXCEEDED"
    assert halted.review_correction_loops == 1
    assert halted.total_correction_loops == 1
    assert "human_commit_approval" not in halted.node_history


def test_commit_approval_mismatch_cancellation_and_clarification_flow(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(db_session, tmp_path)

    staged = iter(
        [
            _make_req_json(needs_clarification=True),
            _make_req_json(needs_clarification=False),
            _make_analysis_json(repo.id, commit_sha),
            _make_planning_json(),
            _make_initial_coding_json(workspace, commit_sha, default_limit=25),
            _make_clean_review_json(),
        ]
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
        orchestrator = _make_orchestrator(db_session, settings, mock_llm_http, mock_sbx_http)

        clarify_pause = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Clarify and commit mismatch",
            request_text="Add pagination.",
            idempotency_key="wf-clarify-commit",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert clarify_pause.status == "waiting_clarification"

        plan_pause = orchestrator.resume_workflow(
            workflow_id=clarify_pause.workflow_id,
            decision=HumanDecision(
                action="clarify",
                actor="admin",
                clarification_answers="Use default limit=25.",
            ),
        )
        assert plan_pause.status == "waiting_plan_approval"
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

        cfg = {"configurable": {"thread_id": commit_pause.thread_id}}
        orchestrator._graph.update_state(cfg, {"state_schema_version": "0.9-old"})
        with pytest.raises(ValidationFailedError):
            orchestrator.resume_workflow(
                workflow_id=commit_pause.workflow_id,
                decision=HumanDecision(
                    action="approve",
                    actor="admin",
                    approved_patch_hash=commit_pause.patch_hash,
                    approved_base_commit_sha=commit_sha,
                ),
            )
        orchestrator._graph.update_state(cfg, {"state_schema_version": "1.0"})

        mismatch = orchestrator.resume_workflow(
            workflow_id=commit_pause.workflow_id,
            decision=HumanDecision(
                action="approve",
                actor="admin",
                approved_patch_hash="0" * 64,
                approved_base_commit_sha=commit_sha,
            ),
        )
        assert mismatch.status == "failed"
        assert mismatch.error_code == "APPROVAL_BINDING_MISMATCH"
        assert "release_preparation" not in mismatch.node_history

        staged_cancel = iter([_make_req_json(needs_clarification=True)])
        orchestrator_cancel = _make_orchestrator(
            db_session,
            settings,
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(
                        200, json={"choices": [{"message": {"content": next(staged_cancel)}}]}
                    )
                )
            ),
            mock_sbx_http,
        )
        paused_for_cancel = orchestrator_cancel.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Cancel test",
            request_text="Add pagination.",
            idempotency_key="wf-cancel-001",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        cancelled = orchestrator_cancel.cancel_workflow(
            workflow_id=paused_for_cancel.workflow_id,
            actor="admin",
            reason="No longer needed",
        )
        assert cancelled.status == "cancelled"
        assert cancelled.interrupted is False
        with pytest.raises(ConflictError):
            orchestrator_cancel.resume_workflow(
                workflow_id=paused_for_cancel.workflow_id,
                decision=HumanDecision(
                    action="clarify",
                    actor="admin",
                    clarification_answers="Too late",
                ),
            )


def test_mandatory_cost_limit_and_llm_call_budget_exhaustion(
    db_session: Session,
    tmp_path: Path,
) -> None:
    repo, workspace, commit_sha, settings = _seed_and_index_demo_repo(
        db_session,
        tmp_path,
        max_llm_calls=1,
    )
    staged = iter([_make_req_json()])

    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200, json={"choices": [{"message": {"content": next(staged)}}]}
                )
            )
        ) as mock_llm_http,
        httpx.Client(transport=_make_sandbox_mock_handler([])) as mock_sbx_http,
    ):
        orchestrator = _make_orchestrator(db_session, settings, mock_llm_http, mock_sbx_http)

        with pytest.raises(ValidationFailedError):
            orchestrator.start_workflow(
                repository=repo,
                base_branch="main",
                base_commit_sha=commit_sha,
                workspace_dir=workspace,
                title="Missing budget",
                request_text="Add pagination.",
                idempotency_key="wf-zero-cost",
                cost_limit_usd=Decimal("0"),
                actor="admin",
            )

        exhausted = orchestrator.start_workflow(
            repository=repo,
            base_branch="main",
            base_commit_sha=commit_sha,
            workspace_dir=workspace,
            title="Call cap test",
            request_text="Add pagination.",
            idempotency_key="wf-call-cap",
            cost_limit_usd=Decimal("5.0000"),
            actor="admin",
        )
        assert exhausted.status == "failed"
        assert exhausted.error_code == "BUDGET_EXHAUSTED"
        assert exhausted.llm_calls_count == 1