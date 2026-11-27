"""Unit and integration tests for ReleaseDeliveryService and PR/artifact/report REST APIs."""

import json
import shutil
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from app.agents.schemas import ReleasePreparationOutput
from app.api.deps import get_artifact_storage, get_github_client, get_workspace_service
from app.core.config import get_settings, load_settings
from app.core.exceptions import AuthorizationError, ConflictError
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.integrations.github.client import GitHubClient
from app.main import create_app
from app.models import (
    ApprovalModel,
    AuditLogModel,
    Base,
    CodeChangeModel,
    CodeReviewModel,
    LLMUsageRecordModel,
    ProjectModel,
    RepositoryModel,
    StaticAnalysisFindingModel,
    TestExecutionModel,
    WorkflowRunModel,
)
from app.services.artifact_storage import ArtifactService, LocalArtifactStorage
from app.services.delivery_service import ReleaseDeliveryService, build_deterministic_branch_name
from app.services.workspace_service import WorkspaceService
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from git import Repo
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_FIXTURE_DIR = REPO_ROOT / "fixtures" / "demo_fastapi_repo"


def _create_bare_remote_and_seed(tmp_path: Path) -> tuple[Path, Path, str, str]:
    bare_remote = tmp_path / "origin.git"
    Repo.init(str(bare_remote), bare=True).close()

    seed_dir = tmp_path / "seed"
    shutil.copytree(DEMO_FIXTURE_DIR, seed_dir)
    seed_repo = Repo.init(str(seed_dir))
    try:
        with seed_repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Seed Dev")
            cfg.set_value("user", "email", "seed@example.com")
        seed_repo.git.add(A=True)
        base_sha = str(seed_repo.index.commit("Initial seed").hexsha)
        default_branch = str(seed_repo.active_branch.name)
        seed_repo.create_remote("origin", str(bare_remote))
        seed_repo.git.push("origin", f"HEAD:refs/heads/{default_branch}")
        return bare_remote, seed_dir, base_sha, default_branch
    finally:
        seed_repo.close()


def test_release_delivery_service_and_rest_apis_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bare_remote, seed_dir, base_sha, default_branch = _create_bare_remote_and_seed(tmp_path)

    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-555")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password("DeliveryPass!2026"))
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
        GITHUB_AUTH_MODE="pat_fallback",
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testtoken1234567890abcdefghij",  # noqa: S106
        WORKSPACE_ROOT_DIR=tmp_path / "managed_ws",
        ARTIFACT_STORAGE_PATH=tmp_path / "artifacts",
    )
    ws_service = WorkspaceService(settings=settings)
    artifact_storage = LocalArtifactStorage(settings=settings)

    remote_branch_sha = base_sha
    created_prs: list[dict[str, object]] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/repos/acme/catalog/branches/{default_branch}":
            return httpx.Response(
                200,
                json={"name": default_branch, "commit": {"sha": remote_branch_sha}},
            )
        if request.method == "GET" and path == "/repos/acme/catalog/pulls":
            return httpx.Response(200, json=created_prs)
        if request.method == "POST" and path == "/repos/acme/catalog/pulls":
            body = json.loads(request.content.decode("utf-8"))
            pr_obj = {
                "number": 17,
                "title": body["title"],
                "body": body["body"],
                "state": "open",
                "draft": body["draft"],
                "html_url": "https://github.com/acme/catalog/pull/17",
                "head": {"ref": body["head"], "sha": "1" * 40},
                "base": {"ref": body["base"], "sha": base_sha},
                "merged": False,
            }
            created_prs.append(pr_obj)
            return httpx.Response(201, json=pr_obj)
        if request.method == "GET" and path == "/repos/acme/catalog/pulls/17":
            return httpx.Response(200, json=created_prs[0])
        if request.method == "GET" and path == "/repos/acme/catalog/pulls/17/reviews":
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "reviewer-1"},
                        "state": "APPROVED",
                        "submitted_at": "2026-11-27T18:00:00Z",
                    }
                ],
            )
        if request.method == "GET" and path == f"/repos/acme/catalog/commits/{'1' * 40}/check-runs":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "ci / pytest",
                            "status": "completed",
                            "conclusion": "success",
                            "details_url": "https://github.com/acme/catalog/runs/99",
                        }
                    ]
                },
            )
        if request.method == "GET" and path == f"/repos/acme/catalog/commits/{'1' * 40}/status":
            return httpx.Response(200, json={"state": "success"})
        return httpx.Response(404)

    class _LocalPushGitHubClient(GitHubClient):
        def build_authenticated_clone_url(
            self,
            clone_url: str,
            *,
            installation_id: str | None = None,
        ) -> str:
            _ = (clone_url, installation_id)
            return str(bare_remote)

    mock_http = httpx.Client(transport=httpx.MockTransport(github_handler))
    gh_client = _LocalPushGitHubClient(settings=settings, http_client=mock_http)

    try:
        # Seed workflow, evidence records, and modified workspace
        with session_factory() as db:
            project = ProjectModel(name="catalog-platform")
            db.add(project)
            db.flush()
            repo = RepositoryModel(
                project_id=project.id,
                name="catalog",
                full_name="acme/catalog",
                provider="github",
                clone_url="https://github.com/acme/catalog.git",
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
                thread_id="wf-deliv-thread-1",
                idempotency_key="wf-deliv-idem-1",
                title="Add pagination to GET /items",
                request_text="Add validated limit/offset pagination.",
                status="completed",
                current_node="release_preparation",
                cost_limit_usd=Decimal("5.0000"),
                created_by="admin",
            )
            db.add(run)
            db.flush()

            db.add(
                CodeChangeModel(
                    workflow_id=run.id,
                    revision_index=0,
                    base_commit_sha=base_sha,
                    patch_hash="c" * 64,
                    files_changed=["README.md"],
                    lines_added=2,
                    lines_removed=1,
                    total_changed_lines=3,
                    unified_diff_preview="+pagination docs\n",
                )
            )
            db.add(
                TestExecutionModel(
                    workflow_id=run.id,
                    run_index=0,
                    sandbox_run_id="sbx-1",
                    base_commit_sha=base_sha,
                    passed=True,
                    install_exit_code=0,
                    test_exit_code=0,
                    duration_seconds=0.35,
                    timed_out=False,
                    network_policy="none",
                    stdout_excerpt="4 passed",
                )
            )
            db.add(
                StaticAnalysisFindingModel(
                    workflow_id=run.id,
                    run_index=0,
                    finding_index=0,
                    tool="ruff",
                    rule_id="E501",
                    severity="Low",
                    file_path="app/main.py",
                    start_line=10,
                    end_line=10,
                    message="Line length hint",
                    confidence="high",
                )
            )
            db.add(
                CodeReviewModel(
                    workflow_id=run.id,
                    review_index=0,
                    base_commit_sha=base_sha,
                    patch_hash="c" * 64,
                    passed=True,
                    summary="Approved after tests and static checks.",
                )
            )
            db.add(
                ApprovalModel(
                    workflow_id=run.id,
                    approval_type="commit",
                    decision="approved",
                    actor="admin",
                    base_commit_sha=base_sha,
                    target_hash="c" * 64,
                )
            )
            db.add(
                LLMUsageRecordModel(
                    workflow_id=run.id,
                    call_index=0,
                    provider="openai",
                    model="gpt-4o-mini",
                    prompt_version="release:v1",
                    input_tokens=400,
                    output_tokens=150,
                    total_tokens=550,
                    estimated_cost_usd=Decimal("0.000150"),
                    cost_status="estimated",
                    latency_seconds=0.4,
                    secrets_redacted=1,
                )
            )
            db.commit()
            wf_id = run.id

        ws_id = f"wf_{wf_id}"
        ws_info = ws_service.prepare_workspace(
            workspace_id=ws_id,
            clone_source=str(bare_remote),
            base_commit_sha=base_sha,
            branch_name=default_branch,
        )
        readme_file = Path(ws_info.workspace_path) / "README.md"
        readme_file.write_text(
            readme_file.read_text(encoding="utf-8") + "\nPagination supported.\n",
            encoding="utf-8",
        )

        release_out = ReleasePreparationOutput(
            pr_title="feat(items): add validated pagination to GET /items",
            pr_description="Adds limit and offset parameters with tests and README docs.",
            release_notes="Added pagination.",
            changed_files_summary=["README.md: documented pagination"],
            test_results_summary="4 passed in isolated sandbox.",
            review_summary="Code review passed.",
            final_report_markdown="# Final Report\n\nAll gates passed.",
        )

        with session_factory() as db:
            delivery = ReleaseDeliveryService(
                db,
                github_client=gh_client,
                workspace_service=ws_service,
                artifact_service=ArtifactService(db, storage=artifact_storage),
            )

            # Denied without human commit approval
            with pytest.raises(AuthorizationError):
                delivery.deliver_pull_request(
                    workflow_id=wf_id,
                    workspace_id=ws_id,
                    base_commit_sha=base_sha,
                    patch_hash="c" * 64,
                    commit_approved=False,
                    release_output=release_out,
                    actor="admin",
                )

            # Valid approved delivery creates branch, commits, pushes, opens PR, persists row
            res1 = delivery.deliver_pull_request(
                workflow_id=wf_id,
                workspace_id=ws_id,
                base_commit_sha=base_sha,
                patch_hash="c" * 64,
                commit_approved=True,
                release_output=release_out,
                actor="admin",
            )
            assert res1.pr_number == 17
            assert res1.reused_existing is False
            assert res1.head_branch == build_deterministic_branch_name(wf_id, release_out.pr_title)

            # Duplicate delivery with same operation_id is strictly idempotent
            res2 = delivery.deliver_pull_request(
                workflow_id=wf_id,
                workspace_id=ws_id,
                base_commit_sha=base_sha,
                patch_hash="c" * 64,
                commit_approved=True,
                release_output=release_out,
                actor="admin",
            )
            assert res2.pull_request_id == res1.pull_request_id
            assert res2.reused_existing is True
            assert len(created_prs) == 1

            art_ids = delivery.persist_workflow_artifacts(
                workflow_id=wf_id,
                created_by="admin",
                unified_diff="--- a/README.md\n+++ b/README.md\n+Pagination supported.\n",
                sandbox_report={"passed": True, "stdout": "4 passed"},
                static_analysis_report={"passed": True, "findings": []},
                code_review_report={"passed": True, "summary": "Clean"},
                release_output=release_out.model_dump(mode="json"),
            )
            assert len(art_ids) == 5
            db.commit()
            pr_id = res1.pull_request_id
            diff_art_id = art_ids[0]

        # Test stale-base detection refuses delivery when remote base branch moved
        remote_branch_sha = "f" * 40
        with session_factory() as db:
            delivery = ReleaseDeliveryService(
                db,
                github_client=gh_client,
                workspace_service=ws_service,
                artifact_service=ArtifactService(db, storage=artifact_storage),
            )
            with pytest.raises(ConflictError):
                delivery.deliver_pull_request(
                    workflow_id=wf_id,
                    workspace_id=ws_id,
                    base_commit_sha=base_sha,
                    patch_hash="d" * 64,
                    commit_approved=True,
                    release_output=release_out,
                    actor="admin",
                )
        remote_branch_sha = base_sha

        # Wire FastAPI app and verify all REST endpoints + authentication
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

        def override_github() -> Generator[GitHubClient, None, None]:
            yield gh_client

        app = create_app()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_github_client] = override_github
        app.dependency_overrides[get_workspace_service] = lambda: ws_service
        app.dependency_overrides[get_artifact_storage] = lambda: artifact_storage

        admin_token, _ = create_access_token(subject="admin")
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        other_token, _ = create_access_token(subject="other-operator")
        other_headers = {"Authorization": f"Bearer {other_token}"}

        with TestClient(app) as client:
            # Unauthenticated access rejected
            assert client.get("/api/v1/pull-requests").status_code == 401
            assert client.get(f"/api/v1/workflows/{wf_id}/artifacts").status_code == 401
            assert client.get(f"/api/v1/workflows/{wf_id}/report").status_code == 401

            # List and inspect PR (with live CI/review refresh)
            prs_resp = client.get(
                f"/api/v1/pull-requests?workflow_id={wf_id}",
                headers=admin_headers,
            )
            assert prs_resp.status_code == 200
            assert prs_resp.json()["total"] == 1

            pr_detail = client.get(
                f"/api/v1/pull-requests/{pr_id}?refresh=true",
                headers=admin_headers,
            )
            assert pr_detail.status_code == 200
            pr_body = pr_detail.json()
            assert pr_body["live_refreshed"] is True
            assert pr_body["pull_request"]["ci_status"] == "success"
            assert pr_body["pull_request"]["review_decision"] == "approved"
            assert len(pr_body["checks"]) == 1
            assert len(pr_body["reviews"]) == 1

            # List artifacts, read metadata, download bytes, and enforce owner authorization
            arts_resp = client.get(
                f"/api/v1/workflows/{wf_id}/artifacts",
                headers=admin_headers,
            )
            assert arts_resp.status_code == 200
            assert arts_resp.json()["total"] == 5

            art_meta = client.get(f"/api/v1/artifacts/{diff_art_id}", headers=admin_headers)
            assert art_meta.status_code == 200
            expected_sha = art_meta.json()["checksum_sha256"]

            art_download = client.get(
                f"/api/v1/artifacts/{diff_art_id}?download=true",
                headers=admin_headers,
            )
            assert art_download.status_code == 200
            assert art_download.headers["X-Artifact-SHA256"] == expected_sha
            assert b"Pagination supported." in art_download.content

            assert (
                client.get(f"/api/v1/artifacts/{diff_art_id}", headers=other_headers).status_code
                == 403
            )

            # Evidence-based workflow report aggregates all tables and final report artifact
            report_resp = client.get(
                f"/api/v1/workflows/{wf_id}/report",
                headers=admin_headers,
            )
            assert report_resp.status_code == 200
            report_body = report_resp.json()
            assert report_body["workflow_id"] == wf_id
            assert report_body["latest_code_change"]["patch_hash"] == "c" * 64
            assert len(report_body["test_executions"]) == 1
            assert len(report_body["static_analysis_findings"]) == 1
            assert report_body["latest_code_review"]["passed"] is True
            assert report_body["pull_request"]["pr_number"] == 17
            assert len(report_body["artifacts"]) == 5
            assert report_body["usage_summary"]["cost_status"] == "estimated"
            assert "All gates passed." in report_body["final_report_markdown"]

        with session_factory() as db:
            actions = set(db.scalars(select(AuditLogModel.action)).all())
        assert "pull_request.deliver" in actions
        assert seed_dir.is_dir()
    finally:
        mock_http.close()
        engine.dispose()
        get_settings.cache_clear()