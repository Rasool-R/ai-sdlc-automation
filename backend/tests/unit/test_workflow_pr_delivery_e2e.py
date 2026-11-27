"""End-to-end worker tests from start through dual approvals to Git push,PR, and report."""

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
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
from app.execution.sandbox_client import SandboxClient
from app.execution.schemas import FilePatchOperation, PatchBundle, SearchReplaceHunk
from app.execution.static_analysis import PipAuditRawOutcome, StaticAnalysisService
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.github.client import GitHubClient
from app.integrations.llm.client import LLMClient
from app.integrations.qdrant.client import QdrantIndexClient
from app.intelligence.discovery import compute_sha256_bytes
from app.models import (
    Base,
    ProjectModel,
    PullRequestModel,
    RepositoryLockModel,
    RepositoryModel,
    WorkflowArtifactModel,
    WorkflowRunModel,
)
from app.services.artifact_storage import ArtifactService, LocalArtifactStorage
from app.services.delivery_service import ReleaseDeliveryService
from app.services.workspace_service import WorkspaceService
from app.workflows.checkpointer import create_memory_checkpointer
from app.workflows.state import HumanDecision, WorkflowExecutionSnapshot
from app.workflows.tasks import (
    execute_workflow_job,
    resume_workflow_job,
    workflow_workspace_id,
)
from cryptography.fernet import Fernet
from git import Repo
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
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


def _init_bare_remote(tmp_path: Path) -> tuple[Path, str, str]:
    bare_remote = tmp_path / "remote.git"
    Repo.init(str(bare_remote), bare=True).close()

    seed_dir = tmp_path / "seed"
    shutil.copytree(DEMO_FIXTURE_DIR, seed_dir)
    seed_repo = Repo.init(str(seed_dir))
    try:
        with seed_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Seed Dev")
            cfg.set_value("user", "email", "seed@example.com")
        seed_repo.git.add(A=True)
        base_sha = str(seed_repo.index.commit("Initial catalog API").hexsha)
        default_branch = str(seed_repo.active_branch.name)
        seed_repo.create_remote("origin", str(bare_remote))
        seed_repo.git.push("origin", f"HEAD:refs/heads/{default_branch}")
        return bare_remote, base_sha, default_branch
    finally:
        seed_repo.close()


def _make_clean_sandbox_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            return httpx.Response(201, json={"run_id": "sbx-e2e", "status": "created"})
        if request.method == "POST" and path == "/runs/sbx-e2e/workspace":
            return httpx.Response(200, json={"run_id": "sbx-e2e", "status": "ready"})
        if request.method == "POST" and path == "/runs/sbx-e2e/execute":
            template = json.loads(request.content.decode("utf-8"))["template"]
            stdout = "4 passed in 0.2s"
            if template == "ruff_check":
                stdout = "[]"
            elif template == "mypy_check":
                stdout = ""
            elif template == "bandit_check":
                stdout = json.dumps({"results": []})
            return httpx.Response(
                200,
                json={
                    "execution_id": f"e-{template}",
                    "template": template,
                    "phase": (
                        "dependency_install"
                        if template == "install_deps"
                        else "isolated_execution"
                    ),
                    "network_policy": "restricted_egress" if template == "install_deps" else "none",
                    "command_argv": [template],
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                    "duration_seconds": 0.1,
                    "timed_out": False,
                },
            )
        if request.method == "DELETE" and path == "/runs/sbx-e2e":
            return httpx.Response(200, json={"run_id": "sbx-e2e", "destroyed": True})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _run_full_worker_pipeline(
    tmp_path: Path,
    *,
    simulate_stale_remote_before_pr: bool = False,
) -> tuple[WorkflowExecutionSnapshot, dict[str, Any]]:
    bare_remote, base_sha, default_branch = _init_bare_remote(tmp_path)

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
        WORKSPACE_ROOT_DIR=tmp_path / "workspaces",
        ARTIFACT_STORAGE_PATH=tmp_path / "artifacts",
        SANDBOX_RUNNER_URL="http://sandbox.internal:8001",
    )

    with session_factory() as db:
        project = ProjectModel(name="e2e-platform")
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
            thread_id="wf-e2e-thread-1",
            idempotency_key="wf-e2e-idem-1",
            title="Add pagination to GET /items",
            request_text="Add validated limit/offset pagination and update README.",
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
    ws_id = workflow_workspace_id(wf_id)
    ws_path = ws_service.root_dir / ws_id

    remote_branch_sha = base_sha
    created_prs: list[dict[str, object]] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if (
            request.method == "GET"
            and path == f"/repos/acme/demo-catalog-api/branches/{default_branch}"
        ):
            return httpx.Response(
                200,
                json={"name": default_branch, "commit": {"sha": remote_branch_sha}},
            )
        if request.method == "GET" and path == "/repos/acme/demo-catalog-api/pulls":
            return httpx.Response(200, json=created_prs)
        if request.method == "POST" and path == "/repos/acme/demo-catalog-api/pulls":
            body = json.loads(request.content.decode("utf-8"))
            pr_obj = {
                "number": 99,
                "title": body["title"],
                "body": body["body"],
                "state": "open",
                "draft": body["draft"],
                "html_url": "https://github.com/acme/demo-catalog-api/pull/99",
                "head": {"ref": body["head"], "sha": "9" * 40},
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
                summary="Add pagination documentation to README.md.",
                functional_requirements=["Document limit and offset query params in README.md."],
                acceptance_criteria=[
                    AcceptanceCriterion(
                        id="AC-1",
                        description="README describes limit and offset.",
                        verification_method="pytest + diff inspection",
                    )
                ],
            ).model_dump_json()
        elif llm_call == 2:
            content = RepositoryAnalysisReport(
                repository_id=repo_id,
                base_commit_sha=base_sha,
                architecture_summary="FastAPI catalog service documented in README.md.",
                relevant_files=["README.md"],
                related_test_files=["tests/test_items.py"],
                evidence=[
                    CodeEvidenceReference(
                        file_path="app/main.py",
                        start_line=17,
                        end_line=21,
                        symbol_name="app.main.list_items",
                        relevance="Catalog list endpoint.",
                        commit_sha=base_sha,
                    )
                ],
            ).model_dump_json()
        elif llm_call == 3:
            content = PlanningOutput(
                design=TechnicalDesign(
                    approach_summary="Document pagination parameters in README.md.",
                    affected_components=["README.md"],
                ),
                plan=ImplementationPlan(
                    summary="Update README.md with pagination usage.",
                    tasks=[
                        PlanTask(
                            task_id="T-1",
                            title="Document pagination params",
                            description="Update GET /items bullet in README.md.",
                            expected_files=["README.md"],
                            acceptance_criteria=["README updated."],
                            verification_steps=["Run sandbox checks."],
                        )
                    ],
                    expected_files=["README.md"],
                ),
            ).model_dump_json()
        elif llm_call == 4:
            readme_hash = compute_sha256_bytes((ws_path / "README.md").read_bytes())
            content = CodingOutput(
                rationale="Documented limit and offset parameters in README.md.",
                patch=PatchBundle(
                    base_commit_sha=base_sha,
                    summary="Document pagination query parameters",
                    operations=[
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
                                        "(`limit`, `offset`) along with the total count."
                                    ),
                                )
                            ],
                        )
                    ],
                ),
            ).model_dump_json()
        elif llm_call == 5:
            content = CodeReviewReport(
                passed=True,
                summary="All checks passed with clean diff.",
                findings=[],
            ).model_dump_json()
        else:
            content = ReleasePreparationOutput(
                pr_title="docs(items): document pagination parameters",
                pr_description="Adds pagination parameter documentation for GET /items.",
                release_notes="Updated README.",
                changed_files_summary=["README.md: documented limit and offset"],
                test_results_summary="Sandbox pytest passed (4 passed).",
                review_summary="Static analysis and review passed.",
                final_report_markdown="# Final Report\n\nVerified end-to-end delivery.",
            ).model_dump_json()

        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
            },
        )

    checkpointer = create_memory_checkpointer()
    try:
        with (
            httpx.Client(transport=httpx.MockTransport(llm_handler)) as mock_llm_http,
            httpx.Client(transport=_make_clean_sandbox_transport()) as mock_sbx_http,
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
                )
                db.commit()
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
                )
                db.commit()
            assert commit_pause.status == "waiting_commit_approval"
            assert commit_pause.patch_hash is not None

            if simulate_stale_remote_before_pr:
                remote_branch_sha = "f" * 40

            with session_factory() as db:
                final_snapshot = resume_workflow_job(
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
                )
                db.commit()

                prs = list(
                    db.scalars(
                        select(PullRequestModel).where(PullRequestModel.workflow_id == wf_id)
                    ).all()
                )
                arts = list(
                    db.scalars(
                        select(WorkflowArtifactModel).where(
                            WorkflowArtifactModel.workflow_id == wf_id
                        )
                    ).all()
                )
                locks = list(db.scalars(select(RepositoryLockModel)).all())

                report = ReleaseDeliveryService(
                    db,
                    github_client=gh_client,
                    workspace_service=ws_service,
                    artifact_service=ArtifactService(db, storage=artifact_storage),
                ).get_workflow_report(wf_id, actor="admin")

        bare_repo = Repo(str(bare_remote))
        try:
            remote_heads = [h.name for h in bare_repo.heads]
        finally:
            bare_repo.close()

        return final_snapshot, {
            "prs_count": len(prs),
            "pr_head_branch": prs[0].head_branch if prs else None,
            "artifacts_count": len(arts),
            "locks_remaining": len(locks),
            "workspace_exists": ws_path.exists(),
            "remote_heads": remote_heads,
            "report_markdown": report.final_report_markdown,
            "report_pr_number": report.pull_request.pr_number if report.pull_request else None,
        }
    finally:
        engine.dispose()


def test_worker_e2e_delivers_git_branch_pull_request_artifacts_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    snapshot, outcome = _run_full_worker_pipeline(tmp_path, simulate_stale_remote_before_pr=False)
    assert snapshot.status == "completed"
    assert snapshot.pr_number == 99
    assert snapshot.pr_url == "https://github.com/acme/demo-catalog-api/pull/99"
    assert len(snapshot.artifact_ids) == 5
    assert outcome["prs_count"] == 1
    assert outcome["artifacts_count"] == 5
    assert outcome["locks_remaining"] == 0
    assert outcome["workspace_exists"] is False
    assert outcome["pr_head_branch"] in outcome["remote_heads"]
    assert outcome["report_pr_number"] == 99
    assert "Verified end-to-end delivery." in str(outcome["report_markdown"])


def test_worker_e2e_blocks_pr_on_stale_remote_base_while_preserving_artifacts(
    tmp_path: Path,
) -> None:
    snapshot, outcome = _run_full_worker_pipeline(tmp_path, simulate_stale_remote_before_pr=True)
    assert snapshot.status == "failed"
    assert snapshot.error_code == "CONFLICT_ERROR"
    assert outcome["prs_count"] == 0
    assert outcome["artifacts_count"] == 5
    assert outcome["locks_remaining"] == 0
    assert outcome["workspace_exists"] is False