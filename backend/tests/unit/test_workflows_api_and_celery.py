"""Unit tests for workflows/tasks REST API endpoints and background Celery job lifecycle."""

import shutil
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.agents.schemas import (
    AcceptanceCriterion,
    CodeEvidenceReference,
    ImplementationPlan,
    PlanningOutput,
    PlanTask,
    RepositoryAnalysisReport,
    RequirementSpecification,
    TechnicalDesign,
)
from app.api.deps import get_github_client, get_workflow_dispatcher
from app.core.config import get_settings, load_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.execution.sandbox_client import SandboxClient
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.github.client import GitHubClient
from app.integrations.llm.client import LLMClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.main import create_app
from app.models import (
    AuditLogModel,
    Base,
    ProjectModel,
    RepositoryLockModel,
    RepositoryModel,
    RepositoryVersionModel,
    TaskDependencyModel,
    TaskModel,
    WorkflowRunModel,
)
from app.services.workspace_service import WorkspaceService
from app.workflows.checkpointer import create_memory_checkpointer
from app.workflows.state import HumanDecision
from app.workflows.tasks import (
    cancel_workflow_job,
    execute_workflow_job,
    resume_workflow_job,
    workflow_workspace_id,
)
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from git import Repo
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_FIXTURE_DIR = REPO_ROOT / "fixtures" / "demo_fastapi_repo"


class _SpyDispatcher:
    """In-memory WorkflowJobDispatcher recording enqueued Celery operations."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def enqueue_start(self, workflow_id: str) -> str:
        self.events.append(("start", workflow_id, {}))
        return f"job-start-{len(self.events)}"

    def enqueue_resume(self, workflow_id: str, decision: HumanDecision) -> str:
        self.events.append(("resume", workflow_id, decision.model_dump(mode="json")))
        return f"job-resume-{len(self.events)}"

    def enqueue_cancel(self, workflow_id: str, actor: str, reason: str | None) -> str:
        self.events.append(("cancel", workflow_id, {"actor": actor, "reason": reason}))
        return f"job-cancel-{len(self.events)}"


@pytest.fixture()
def workflow_api_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[
    tuple[TestClient, sessionmaker[Session], _SpyDispatcher, dict[str, str], str],
    None,
    None,
]:
    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-444")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password("WorkflowPass!2026"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", fernet_key)
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with session_factory() as seed_db:
        project = ProjectModel(name="api-wf-proj", description="Workflow API tests")
        seed_db.add(project)
        seed_db.flush()
        repo = RepositoryModel(
            project_id=project.id,
            name="orders-api",
            full_name="acme/orders-api",
            provider="github",
            clone_url="https://github.com/acme/orders-api.git",
            default_branch="main",
            is_private=True,
            llm_data_sharing=False,
        )
        seed_db.add(repo)
        seed_db.flush()
        seed_db.add(
            RepositoryVersionModel(
                repository_id=repo.id,
                branch_name="main",
                commit_sha="a" * 40,
            )
        )
        seed_db.commit()
        repo_id = repo.id

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    gh_settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        GITHUB_AUTH_MODE="pat_fallback",
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testtoken1234567890abcdefghij",  # noqa: S106
    )
    mock_http = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={"message": "unused"}))
    )
    spy_dispatcher = _SpyDispatcher()

    def override_github() -> Generator[GitHubClient, None, None]:
        yield GitHubClient(settings=gh_settings, http_client=mock_http)

    def override_dispatcher() -> _SpyDispatcher:
        return spy_dispatcher

    application = create_app()
    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_github_client] = override_github
    application.dependency_overrides[get_workflow_dispatcher] = override_dispatcher

    token, _ = create_access_token(subject="admin")
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(application) as client:
        yield client, session_factory, spy_dispatcher, headers, repo_id

    mock_http.close()
    engine.dispose()
    get_settings.cache_clear()


def test_workflow_and_task_endpoints_require_authentication(
    workflow_api_env: tuple[TestClient, sessionmaker[Session], _SpyDispatcher, dict[str, str], str],
) -> None:
    client, _sf, _disp, _headers, repo_id = workflow_api_env
    assert client.get("/api/v1/workflows").status_code == 401
    assert (
        client.post(
            "/api/v1/workflows",
            json={
                "repository_id": repo_id,
                "title": "Add pagination",
                "request_text": "Add limit and offset.",
                "cost_limit_usd": "5.00",
            },
        ).status_code
        == 401
    )
    assert client.get("/api/v1/tasks/any-id").status_code == 401


def test_workflow_api_create_idempotency_control_actions_and_tasks(
    workflow_api_env: tuple[TestClient, sessionmaker[Session], _SpyDispatcher, dict[str, str], str],
) -> None:
    client, session_factory, dispatcher, headers, repo_id = workflow_api_env

    # Mandatory positive cost limit enforced at schema boundary
    bad_budget = client.post(
        "/api/v1/workflows",
        json={
            "repository_id": repo_id,
            "title": "Zero budget",
            "request_text": "Should fail validation.",
            "cost_limit_usd": "0",
        },
        headers=headers,
    )
    assert bad_budget.status_code == 422

    # Create workflow enqueues asynchronously via dispatcher without running graph in handler
    create_resp = client.post(
        "/api/v1/workflows",
        json={
            "repository_id": repo_id,
            "title": "Add pagination",
            "request_text": "Add validated limit/offset to GET /items.",
            "idempotency_key": "idem-api-001",
            "cost_limit_usd": "5.00",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    wf_id = created["workflow_id"]
    assert created["status"] == "pending"
    assert created["job_id"] == "job-start-1"
    assert created["reused_existing"] is False
    assert len(dispatcher.events) == 1

    # Duplicate idempotency_key returns existing workflow without enqueuing duplicate job
    dup_resp = client.post(
        "/api/v1/workflows",
        json={
            "repository_id": repo_id,
            "title": "Add pagination",
            "request_text": "Duplicate request.",
            "idempotency_key": "idem-api-001",
            "cost_limit_usd": "5.00",
        },
        headers=headers,
    )
    assert dup_resp.status_code == 201
    assert dup_resp.json()["workflow_id"] == wf_id
    assert dup_resp.json()["reused_existing"] is True
    assert len(dispatcher.events) == 1

    # List and detail endpoints reflect persisted projection
    list_resp = client.get(f"/api/v1/workflows?repository_id={repo_id}", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    detail_resp = client.get(f"/api/v1/workflows/{wf_id}", headers=headers)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["base_commit_sha"] == "a" * 40

    # Simulate paused waiting_clarification state -> test /resume
    with session_factory() as db:
        run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == wf_id))
        assert run is not None
        run.status = "waiting_clarification"
        db.commit()

    resume_resp = client.post(
        f"/api/v1/workflows/{wf_id}/resume",
        json={"clarification_answers": "Use default limit=25."},
        headers=headers,
    )
    assert resume_resp.status_code == 202
    assert dispatcher.events[-1][0] == "resume"
    assert dispatcher.events[-1][2]["action"] == "clarify"

    # Simulate waiting_plan_approval state -> validate base SHA binding and /approve
    with session_factory() as db:
        run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == wf_id))
        assert run is not None
        run.status = "waiting_plan_approval"
        t1 = TaskModel(
            workflow_id=wf_id,
            task_key="T-1",
            title="Implement pagination",
            description="Add Query params to list_items.",
            expected_files=["app/main.py"],
            acceptance_criteria=["Slices correctly."],
            verification_steps=["Run pytest."],
        )
        t2 = TaskModel(
            workflow_id=wf_id,
            task_key="T-2",
            title="Add tests",
            description="Test pagination bounds.",
            expected_files=["tests/test_items.py"],
            acceptance_criteria=["422 returned on limit=0."],
            verification_steps=["Run pytest."],
        )
        db.add_all([t1, t2])
        db.flush()
        db.add(TaskDependencyModel(workflow_id=wf_id, task_id=t2.id, depends_on_task_id=t1.id))
        db.commit()
        t2_id = t2.id

    # Mismatched base_commit_sha rejected with 409
    bad_sha = client.post(
        f"/api/v1/workflows/{wf_id}/approve",
        json={
            "approved_base_commit_sha": "f" * 40,
            "approved_plan_hash": "b" * 64,
        },
        headers=headers,
    )
    assert bad_sha.status_code == 409

    # Valid plan approval enqueues resume job with plan hash
    approve_resp = client.post(
        f"/api/v1/workflows/{wf_id}/approve",
        json={
            "approved_base_commit_sha": "a" * 40,
            "approved_plan_hash": "b" * 64,
        },
        headers=headers,
    )
    assert approve_resp.status_code == 202
    assert dispatcher.events[-1][2]["approved_plan_hash"] == "b" * 64

    # Task list and individual task endpoints resolve dependencies
    tasks_resp = client.get(f"/api/v1/workflows/{wf_id}/tasks", headers=headers)
    assert tasks_resp.status_code == 200
    assert tasks_resp.json()["total"] == 2

    single_task_resp = client.get(f"/api/v1/tasks/{t2_id}", headers=headers)
    assert single_task_resp.status_code == 200
    assert single_task_resp.json()["depends_on_task_keys"] == ["T-1"]

    # Reject endpoint works while paused
    reject_resp = client.post(
        f"/api/v1/workflows/{wf_id}/reject",
        json={"reason": "Hold for redesign"},
        headers=headers,
    )
    assert reject_resp.status_code == 202
    assert dispatcher.events[-1][2]["action"] == "reject"

    # Cancel endpoint marks cancelled and prevents duplicate cancel
    cancel_resp = client.post(
        f"/api/v1/workflows/{wf_id}/cancel",
        json={"reason": "Operator cancelled"},
        headers=headers,
    )
    assert cancel_resp.status_code == 202
    assert cancel_resp.json()["status"] == "cancelled"
    assert dispatcher.events[-1][0] == "cancel"

    repeat_cancel = client.post(
        f"/api/v1/workflows/{wf_id}/cancel",
        json={"reason": "Again"},
        headers=headers,
    )
    assert repeat_cancel.status_code == 409

    with session_factory() as db:
        actions = set(db.scalars(select(AuditLogModel.action)).all())
    assert {
        "workflow.enqueue",
        "workflow.resume.enqueued",
        "workflow.approve.enqueued",
        "workflow.reject.enqueued",
        "workflow.cancel.enqueued",
    }.issubset(actions)


def test_worker_jobs_manage_locks_workspaces_pause_and_cleanup(tmp_path: Path) -> None:
    # Create a real local Git repository cloned by WorkspaceService during worker execution
    origin_dir = tmp_path / "origin_repo"
    shutil.copytree(DEMO_FIXTURE_DIR, origin_dir)
    git_repo = Repo.init(str(origin_dir))
    try:
        with git_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Worker Tester")
            cfg.set_value("user", "email", "worker@example.com")
        git_repo.git.add(A=True)
        commit_sha = str(git_repo.index.commit("Initial commit").hexsha)
        default_branch = str(git_repo.active_branch.name)
    finally:
        git_repo.close()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        LLM_PROVIDER="local",
        LLM_MODEL_DEFAULT="local-coder",
        LLM_BASE_URL="http://localhost:11434/v1",
        EMBEDDING_PROVIDER="disabled",
        GITHUB_AUTH_MODE="pat_fallback",
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testtoken1234567890abcdefghij",  # noqa: S106
        WORKSPACE_ROOT_DIR=tmp_path / "worker_workspaces",
    )

    class _LocalCloneGitHub(GitHubClient):
        def build_authenticated_clone_url(
            self,
            clone_url: str,
            *,
            installation_id: str | None = None,
        ) -> str:
            _ = (clone_url, installation_id)
            return str(origin_dir)

    with session_factory() as db:
        project = ProjectModel(name="worker-proj")
        db.add(project)
        db.flush()
        repo = RepositoryModel(
            project_id=project.id,
            name="demo-catalog-api",
            full_name="acme/demo-catalog-api",
            provider="github",
            clone_url="https://github.com/acme/demo-catalog-api.git",
            default_branch=default_branch,
            is_private=True,
            llm_data_sharing=False,
        )
        db.add(repo)
        db.flush()
        run = WorkflowRunModel(
            repository_id=repo.id,
            base_branch=default_branch,
            base_commit_sha=commit_sha,
            thread_id="wf-worker-thread-1",
            idempotency_key="worker-idem-1",
            title="Add pagination",
            request_text="Add pagination to GET /items.",
            status="pending",
            current_node="queued",
            cost_limit_usd=Decimal("5.0000"),
            created_by="admin",
        )
        db.add(run)
        db.commit()
        wf_id = run.id
        repo_id = repo.id

    staged = iter(
        [
            RequirementSpecification(
                summary="Add pagination to GET /items.",
                functional_requirements=["Add limit and offset query parameters."],
                acceptance_criteria=[
                    AcceptanceCriterion(
                        id="AC-1",
                        description="GET /items?limit=5 works.",
                        verification_method="pytest",
                    )
                ],
            ).model_dump_json(),
            RepositoryAnalysisReport(
                repository_id=repo_id,
                base_commit_sha=commit_sha,
                architecture_summary="FastAPI catalog service in app/main.py.",
                relevant_files=["app/main.py"],
                evidence=[
                    CodeEvidenceReference(
                        file_path="app/main.py",
                        start_line=1,
                        end_line=20,
                        symbol_name="app.main.list_items",
                        relevance="Target endpoint.",
                        commit_sha=commit_sha,
                    )
                ],
            ).model_dump_json(),
            PlanningOutput(
                design=TechnicalDesign(
                    approach_summary="Add Query-validated limit/offset to list_items.",
                    affected_components=["app/main.py"],
                ),
                plan=ImplementationPlan(
                    summary="Update app/main.py.",
                    tasks=[
                        PlanTask(
                            task_id="T-1",
                            title="Add pagination",
                            description="Update list_items.",
                            expected_files=["app/main.py"],
                            acceptance_criteria=["Works"],
                            verification_steps=["pytest"],
                        )
                    ],
                    expected_files=["app/main.py"],
                ),
            ).model_dump_json(),
        ]
    )

    checkpointer = create_memory_checkpointer()
    ws_service = WorkspaceService(settings=settings)
    ws_path = ws_service.root_dir / workflow_workspace_id(wf_id)

    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": next(staged)}}]},
                )
            )
        ) as mock_llm_http,
        httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(404))
        ) as mock_unused_http,
    ):
        gh = _LocalCloneGitHub(settings=settings, http_client=mock_unused_http)
        llm = LLMClient(settings=settings, http_client=mock_llm_http)
        emb = EmbeddingClient(settings=settings)
        qdrant = QdrantIndexClient(settings=settings)
        sbx = SandboxClient(settings=settings, http_client=mock_unused_http)

        # 1. Start job clones, indexes, pauses at plan approval, releases lock, keeps workspace
        with session_factory() as db:
            paused = execute_workflow_job(
                db=db,
                workflow_id=wf_id,
                settings=settings,
                github_client=gh,
                workspace_service=ws_service,
                llm_client=llm,
                embedding_client=emb,
                qdrant_client=qdrant,
                sandbox_client=sbx,
                checkpointer=checkpointer,
            )
            db.commit()
            remaining_locks = list(db.scalars(select(RepositoryLockModel)).all())

        assert paused.status == "waiting_plan_approval"
        assert remaining_locks == []
        assert ws_path.is_dir()

        # 2. Resume with rejection reaches terminal status -> releases lock & cleans up workspace
        with session_factory() as db:
            rejected = resume_workflow_job(
                db=db,
                workflow_id=wf_id,
                decision=HumanDecision(action="reject", actor="admin", reason="Deferred"),
                settings=settings,
                github_client=gh,
                workspace_service=ws_service,
                llm_client=llm,
                embedding_client=emb,
                qdrant_client=qdrant,
                sandbox_client=sbx,
                checkpointer=checkpointer,
            )
            db.commit()
            remaining_locks_after = list(db.scalars(select(RepositoryLockModel)).all())

        assert rejected.status == "rejected"
        assert remaining_locks_after == []
        assert not ws_path.exists()

    # 3. Verify cancel_workflow_job cleans workspace cleanly for a queued workflow
    with session_factory() as db:
        queued = WorkflowRunModel(
            repository_id=repo_id,
            base_branch=default_branch,
            base_commit_sha=commit_sha,
            thread_id="wf-worker-thread-2",
            idempotency_key="worker-idem-2",
            title="Cancel check",
            request_text="Cancel before run.",
            status="pending",
            current_node="queued",
            cost_limit_usd=Decimal("5.0000"),
            created_by="admin",
        )
        db.add(queued)
        db.commit()
        queued_id = queued.id

    with session_factory() as db:
        cancelled = cancel_workflow_job(
            db=db,
            workflow_id=queued_id,
            actor="admin",
            reason="Aborted",
            settings=settings,
            workspace_service=ws_service,
            checkpointer=checkpointer,
        )
        db.commit()
    assert cancelled.status == "cancelled"
    engine.dispose()