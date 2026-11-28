"""Integration tests verifying automatic live workflow event emission across API and workers."""

import json
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
from app.api.deps import get_github_client, get_workflow_dispatcher
from app.core.config import get_settings, load_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.execution.sandbox_client import SandboxClient
from app.execution.schemas import FilePatchOperation, PatchBundle, SearchReplaceHunk
from app.execution.static_analysis import PipAuditRawOutcome, StaticAnalysisService
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.github.client import GitHubClient
from app.integrations.llm.client import LLMClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.discovery import compute_sha256_bytes
from app.main import create_app
from app.models import (
    Base,
    ProjectModel,
    RepositoryModel,
    RepositoryVersionModel,
    WorkflowEventModel,
    WorkflowRunModel,
)
from app.services.artifact_storage import LocalArtifactStorage
from app.services.workspace_service import WorkspaceService
from app.workflows.checkpointer import create_memory_checkpointer
from app.workflows.state import HumanDecision
from app.workflows.tasks import (
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


class _CleanPipAuditStub:
    def audit_requirements_file(self, requirements_path: Path) -> PipAuditRawOutcome:
        _ = requirements_path
        return PipAuditRawOutcome(
            available=True,
            exit_code=0,
            stdout=json.dumps({"dependencies": []}),
            stderr="",
            duration_seconds=0.03,
        )


class _SpyDispatcher:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def enqueue_start(self, workflow_id: str) -> str:
        self.events.append(("start", workflow_id))
        return f"job-start-{len(self.events)}"

    def enqueue_resume(self, workflow_id: str, decision: HumanDecision) -> str:
        self.events.append((f"resume:{decision.action}", workflow_id))
        return f"job-resume-{len(self.events)}"

    def enqueue_cancel(self, workflow_id: str, actor: str, reason: str | None) -> str:
        _ = (actor, reason)
        self.events.append(("cancel", workflow_id))
        return f"job-cancel-{len(self.events)}"


def _init_bare_remote(tmp_path: Path) -> tuple[Path, str, str]:
    bare_remote = tmp_path / "remote.git"
    Repo.init(str(bare_remote), bare=True).close()

    seed_dir = tmp_path / "seed"
    shutil.copytree(DEMO_FIXTURE_DIR, seed_dir)
    seed_repo = Repo.init(str(seed_dir))
    try:
        with seed_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Event Dev")
            cfg.set_value("user", "email", "event@example.com")
        seed_repo.git.add(A=True)
        base_sha = str(seed_repo.index.commit("Initial seed").hexsha)
        default_branch = str(seed_repo.active_branch.name)
        seed_repo.create_remote("origin", str(bare_remote))
        seed_repo.git.push("origin", f"HEAD:refs/heads/{default_branch}")
        return bare_remote, base_sha, default_branch
    finally:
        seed_repo.close()


def test_workflow_service_actions_emit_monotonic_events_and_avoid_idempotent_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-777")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password("LiveEvtPass!2026"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", fernet_key)
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with session_factory() as db:
        project = ProjectModel(name="evt-svc-proj")
        db.add(project)
        db.flush()
        repo = RepositoryModel(
            project_id=project.id,
            name="orders",
            full_name="acme/orders",
            provider="github",
            clone_url="https://github.com/acme/orders.git",
            default_branch="main",
            is_private=True,
            llm_data_sharing=False,
        )
        db.add(repo)
        db.flush()
        db.add(
            RepositoryVersionModel(
                repository_id=repo.id,
                branch_name="main",
                commit_sha="a" * 40,
            )
        )
        db.commit()
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
    mock_http = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
    spy_dispatcher = _SpyDispatcher()

    def override_github() -> Generator[GitHubClient, None, None]:
        yield GitHubClient(settings=gh_settings, http_client=mock_http)

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_github_client] = override_github
    app.dependency_overrides[get_workflow_dispatcher] = lambda: spy_dispatcher

    token, _ = create_access_token(subject="admin")
    headers = {"Authorization": f"Bearer {token}"}

    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/workflows",
                json={
                    "repository_id": repo_id,
                    "title": "Add filters",
                    "request_text": "Add status filters to list endpoint.",
                    "idempotency_key": "live-evt-idem-1",
                    "cost_limit_usd": "5.00",
                },
                headers=headers,
            )
            assert created.status_code == 201
            wf_id = created.json()["workflow_id"]

            # Duplicate create must not emit a duplicate workflow.enqueued event
            dup = client.post(
                "/api/v1/workflows",
                json={
                    "repository_id": repo_id,
                    "title": "Add filters",
                    "request_text": "Duplicate.",
                    "idempotency_key": "live-evt-idem-1",
                    "cost_limit_usd": "5.00",
                },
                headers=headers,
            )
            assert dup.status_code == 201
            assert dup.json()["reused_existing"] is True

            with session_factory() as db:
                run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == wf_id))
                assert run is not None
                run.status = "waiting_clarification"
                db.commit()

            assert (
                client.post(
                    f"/api/v1/workflows/{wf_id}/resume",
                    json={"clarification_answers": "Filter by active only."},
                    headers=headers,
                ).status_code
                == 202
            )

            with session_factory() as db:
                run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == wf_id))
                assert run is not None
                run.status = "waiting_plan_approval"
                db.commit()

            assert (
                client.post(
                    f"/api/v1/workflows/{wf_id}/approve",
                    json={
                        "approved_base_commit_sha": "a" * 40,
                        "approved_plan_hash": "b" * 64,
                    },
                    headers=headers,
                ).status_code
                == 202
            )

            assert (
                client.post(
                    f"/api/v1/workflows/{wf_id}/cancel",
                    json={"reason": "Cancelled with token ghp_1234567890abcdefghijklmnopqrstuv"},
                    headers=headers,
                ).status_code
                == 202
            )

            events_resp = client.get(f"/api/v1/workflows/{wf_id}/events", headers=headers)
            assert events_resp.status_code == 200
            items = events_resp.json()["items"]
            assert [item["event_type"] for item in items] == [
                "workflow.enqueued",
                "human.clarification_submitted",
                "human.approval_submitted",
                "workflow.cancelled",
            ]
            assert [item["id"] for item in items] == sorted(item["id"] for item in items)
            assert "ghp_1234567890abcdefghijklmnopqrstuv" not in items[-1]["message"]
            assert "[REDACTED]" in items[-1]["message"]
    finally:
        mock_http.close()
        engine.dispose()
        get_settings.cache_clear()


def test_worker_streams_per_node_events_across_correction_loop_and_sse_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bare_remote, base_sha, default_branch = _init_bare_remote(tmp_path)

    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-888")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password("StreamPass!2026"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", fernet_key)
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        LLM_PROVIDER="local",
        LLM_MODEL_DEFAULT="local-coder",
        LLM_BASE_URL="http://localhost:11434/v1",
        EMBEDDING_PROVIDER="disabled",
        GITHUB_AUTH_MODE="pat_fallback",
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testtoken1234567890abcdefghij",  # noqa: S106
        WORKSPACE_ROOT_DIR=tmp_path / "workspaces",
        ARTIFACT_STORAGE_PATH=tmp_path / "artifacts",
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )

    with session_factory() as db:
        project = ProjectModel(name="stream-platform")
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
            base_commit_sha=base_sha,
            thread_id="wf-stream-thread-1",
            idempotency_key="wf-stream-idem-1",
            title="Document pagination",
            request_text="Document limit and offset parameters in README.md.",
            status="pending",
            current_node="queued",
            cost_limit_usd=Decimal("5.0000"),
            created_by="admin",
        )
        db.add(run)
        db.commit()
        wf_id = run.id
        repo_id = repo.id

    ws_service = WorkspaceService(settings=settings)
    artifact_storage = LocalArtifactStorage(settings=settings)
    ws_path = ws_service.root_dir / workflow_workspace_id(wf_id)

    pytest_codes = [1, 0]

    def sandbox_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            return httpx.Response(201, json={"run_id": "sbx-live", "status": "created"})
        if request.method == "POST" and path == "/runs/sbx-live/workspace":
            return httpx.Response(200, json={"run_id": "sbx-live", "status": "ready"})
        if request.method == "POST" and path == "/runs/sbx-live/execute":
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
                        "duration_seconds": 0.1,
                        "timed_out": False,
                    },
                )
            if template == "pytest":
                code = pytest_codes.pop(0) if len(pytest_codes) > 1 else pytest_codes[0]
                return httpx.Response(
                    200,
                    json={
                        "execution_id": f"e-pytest-{code}",
                        "template": "pytest",
                        "phase": "isolated_execution",
                        "network_policy": "none",
                        "command_argv": ["pytest"],
                        "exit_code": code,
                        "stdout": "4 passed" if code == 0 else "1 failed",
                        "stderr": "" if code == 0 else "AssertionError",
                        "duration_seconds": 0.1,
                        "timed_out": False,
                    },
                )
            stdout = "[]" if template == "ruff_check" else ""
            if template == "bandit_check":
                stdout = json.dumps({"results": []})
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
        if request.method == "DELETE" and path == "/runs/sbx-live":
            return httpx.Response(200, json={"run_id": "sbx-live", "destroyed": True})
        return httpx.Response(404)

    created_prs: list[dict[str, Any]] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if (
            request.method == "GET"
            and path == f"/repos/acme/demo-catalog-api/branches/{default_branch}"
        ):
            return httpx.Response(200, json={"name": default_branch, "commit": {"sha": base_sha}})
        if request.method == "GET" and path == "/repos/acme/demo-catalog-api/pulls":
            return httpx.Response(200, json=created_prs)
        if request.method == "POST" and path == "/repos/acme/demo-catalog-api/pulls":
            body = json.loads(request.content.decode("utf-8"))
            pr_obj = {
                "number": 77,
                "title": body["title"],
                "body": body["body"],
                "state": "open",
                "draft": body["draft"],
                "html_url": "https://github.com/acme/demo-catalog-api/pull/77",
                "head": {"ref": body["head"], "sha": "7" * 40},
                "base": {"ref": body["base"], "sha": base_sha},
                "merged": False,
            }
            created_prs.append(pr_obj)
            return httpx.Response(201, json=pr_obj)
        return httpx.Response(404)

    class _LocalClonePushGitHub(GitHubClient):
        def build_authenticated_clone_url(
            self,
            clone_url: str,
            *,
            installation_id: str | None = None,
        ) -> str:
            _ = (clone_url, installation_id)
            return str(bare_remote)

    llm_call = 0

    def llm_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal llm_call
        llm_call += 1
        if llm_call == 1:
            content = RequirementSpecification(
                summary="Document pagination parameters in README.md.",
                functional_requirements=["Add limit and offset description to README.md."],
                acceptance_criteria=[
                    AcceptanceCriterion(
                        id="AC-1",
                        description="README mentions limit and offset.",
                        verification_method="pytest",
                    )
                ],
            ).model_dump_json()
        elif llm_call == 2:
            content = RepositoryAnalysisReport(
                repository_id=repo_id,
                base_commit_sha=base_sha,
                architecture_summary="FastAPI catalog service.",
                relevant_files=["README.md"],
                evidence=[
                    CodeEvidenceReference(
                        file_path="app/main.py",
                        start_line=17,
                        end_line=21,
                        symbol_name="app.main.list_items",
                        relevance="Target endpoint.",
                        commit_sha=base_sha,
                    )
                ],
            ).model_dump_json()
        elif llm_call == 3:
            content = PlanningOutput(
                design=TechnicalDesign(
                    approach_summary="Document pagination params in README.md.",
                    affected_components=["README.md"],
                ),
                plan=ImplementationPlan(
                    summary="Update README.md.",
                    tasks=[
                        PlanTask(
                            task_id="T-1",
                            title="Update README",
                            description="Document pagination params.",
                            expected_files=["README.md"],
                            acceptance_criteria=["README updated."],
                            verification_steps=["Run sandbox."],
                        )
                    ],
                    expected_files=["README.md"],
                ),
            ).model_dump_json()
        elif llm_call == 4:
            h = compute_sha256_bytes((ws_path / "README.md").read_bytes())
            content = CodingOutput(
                rationale="Initial README update.",
                patch=PatchBundle(
                    base_commit_sha=base_sha,
                    summary="Initial README update",
                    operations=[
                        FilePatchOperation(
                            path="README.md",
                            operation="modify",
                            base_content_hash=h,
                            hunks=[
                                SearchReplaceHunk(
                                    search=(
                                        "- `GET /items` — Lists all catalog items "
                                        "along with the total count."
                                    ),
                                    replace=(
                                        "- `GET /items` — Lists paginated catalog items "
                                        "(`limit`) along with the total count."
                                    ),
                                )
                            ],
                        )
                    ],
                ),
            ).model_dump_json()
        elif llm_call == 5:
            content = TestFailureDiagnosis(
                summary="Pagination docs omitted offset parameter.",
                failure_category="assertion_failure",
                root_cause="Missing offset in README.md.",
                failing_tests=["tests/test_items.py::test_docs"],
                affected_files=["README.md"],
                recommended_fixes=["Include offset alongside limit."],
                correction_instructions="Add offset to GET /items bullet in README.md.",
                is_fixable_by_coding_agent=True,
            ).model_dump_json()
        elif llm_call == 6:
            h = compute_sha256_bytes((ws_path / "README.md").read_bytes())
            content = CodingOutput(
                rationale="Added offset alongside limit.",
                patch=PatchBundle(
                    base_commit_sha=base_sha,
                    summary="Add offset parameter documentation",
                    operations=[
                        FilePatchOperation(
                            path="README.md",
                            operation="modify",
                            base_content_hash=h,
                            hunks=[
                                SearchReplaceHunk(
                                    search="(`limit`) along with the total count.",
                                    replace="(`limit`, `offset`) along with the total count.",
                                )
                            ],
                        )
                    ],
                ),
            ).model_dump_json()
        elif llm_call == 7:
            content = CodeReviewReport(
                passed=True,
                summary="Verified clean.",
                findings=[],
            ).model_dump_json()
        else:
            content = ReleasePreparationOutput(
                pr_title="docs(items): document limit and offset pagination",
                pr_description="Documents pagination query params.",
                release_notes="Updated README.",
                changed_files_summary=["README.md: documented limit and offset"],
                test_results_summary="Sandbox tests passed.",
                review_summary="Review passed.",
                final_report_markdown="# Report\n\nLive events verified.",
            ).model_dump_json()

        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    checkpointer = create_memory_checkpointer()
    try:
        with (
            httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
            httpx.Client(transport=httpx.MockTransport(sandbox_handler)) as mock_sbx_http,
            httpx.Client(transport=httpx.MockTransport(github_handler)) as mock_gh_http,
        ):
            gh_client = _LocalClonePushGitHub(settings=settings, http_client=mock_gh_http)
            llm_client = LLMClient(settings=settings, http_client=mock_llm_http)
            emb_client = EmbeddingClient(settings=settings)
            qdrant_client = QdrantIndexClient(settings=settings)
            sbx_client = SandboxClient(settings=settings, http_client=mock_sbx_http)
            static_svc = StaticAnalysisService(
                sandbox_client=sbx_client,
                pip_audit_runner=_CleanPipAuditStub(),
            )

            with session_factory() as db:
                plan_pause = execute_workflow_job(
                    db=db,
                    workflow_id=wf_id,
                    settings=settings,
                    github_client=gh_client,
                    workspace_service=ws_service,
                    artifact_storage=artifact_storage,
                    llm_client=llm_client,
                    embedding_client=emb_client,
                    qdrant_client=qdrant_client,
                    sandbox_client=sbx_client,
                    static_analysis_service=static_svc,
                    checkpointer=checkpointer,
                    commit_progress=True,
                )
            assert plan_pause.status == "waiting_plan_approval"
            assert plan_pause.plan_hash is not None

            with session_factory() as db:
                commit_pause = resume_workflow_job(
                    db=db,
                    workflow_id=wf_id,
                    decision=HumanDecision(
                        action="approve",
                        actor="admin",
                        approved_plan_hash=plan_pause.plan_hash,
                        approved_base_commit_sha=base_sha,
                    ),
                    settings=settings,
                    github_client=gh_client,
                    workspace_service=ws_service,
                    artifact_storage=artifact_storage,
                    llm_client=llm_client,
                    embedding_client=emb_client,
                    qdrant_client=qdrant_client,
                    sandbox_client=sbx_client,
                    static_analysis_service=static_svc,
                    checkpointer=checkpointer,
                    commit_progress=True,
                )
            assert commit_pause.status == "waiting_commit_approval"
            assert commit_pause.patch_hash is not None

            with session_factory() as db:
                done = resume_workflow_job(
                    db=db,
                    workflow_id=wf_id,
                    decision=HumanDecision(
                        action="approve",
                        actor="admin",
                        approved_patch_hash=commit_pause.patch_hash,
                        approved_base_commit_sha=base_sha,
                    ),
                    settings=settings,
                    github_client=gh_client,
                    workspace_service=ws_service,
                    artifact_storage=artifact_storage,
                    llm_client=llm_client,
                    embedding_client=emb_client,
                    qdrant_client=qdrant_client,
                    sandbox_client=sbx_client,
                    static_analysis_service=static_svc,
                    checkpointer=checkpointer,
                    commit_progress=True,
                )
            assert done.status == "completed"
            assert done.pr_number == 77

        # Verify persisted monotonic timeline distinguishes failed vs passing sandbox runs
        with session_factory() as db:
            rows = list(
                db.scalars(
                    select(WorkflowEventModel)
                    .where(WorkflowEventModel.workflow_id == wf_id)
                    .order_by(WorkflowEventModel.id.asc())
                ).all()
            )

        sandbox_events = [
            r for r in rows if r.event_type == "node.executed" and r.node_name == "sandbox_test"
        ]
        assert len(sandbox_events) == 2
        assert sandbox_events[0].payload["passed"] is False
        assert sandbox_events[1].payload["passed"] is True

        human_events = [r for r in rows if r.event_type == "human.action_required"]
        assert [r.status for r in human_events] == [
            "waiting_plan_approval",
            "waiting_commit_approval",
        ]
        assert rows[-1].event_type == "workflow.completed"
        assert rows[-1].payload["pr_number"] == 77

        # Verify SSE endpoint replays the entire persisted timeline cleanly
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

        app = create_app()
        app.dependency_overrides[get_db] = override_get_db
        token, _ = create_access_token(subject="admin")
        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(app) as client, client.stream(
            "GET",
            f"/api/v1/workflows/{wf_id}/events?stream=true&poll_interval_seconds=0.05",
            headers=headers,
        ) as sse_resp:
            assert sse_resp.status_code == 200
            stream_body = "".join(sse_resp.iter_text())

        assert "event: human.action_required" in stream_body
        assert "event: workflow.completed" in stream_body
        assert ": workflow_terminal:completed" in stream_body
    finally:
        engine.dispose()
        get_settings.cache_clear()