"""Unit and integration tests for ArtifactStorage, GitHub PR/CI client, and safe Git push."""

import json
import shutil
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from git import Repo
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import load_settings
from app.core.exceptions import AuthorizationError, ConflictError, ValidationFailedError
from app.integrations.github.client import GitHubClient
from app.models import Base, ProjectModel, RepositoryModel, WorkflowRunModel
from app.services.artifact_storage import ArtifactService, LocalArtifactStorage
from app.services.workspace_service import WorkspaceService

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


def _seed_workflow(db: Session) -> WorkflowRunModel:
    project = ProjectModel(name="delivery-proj")
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
    run = WorkflowRunModel(
        repository_id=repo.id,
        base_branch="main",
        base_commit_sha="a" * 40,
        thread_id="wf-art-thread-1",
        idempotency_key="wf-art-idem-1",
        title="Add pagination",
        request_text="Add pagination to GET /items.",
        status="completed",
        current_node="release_preparation",
        cost_limit_usd=Decimal("5.0000"),
        created_by="admin",
    )
    db.add(run)
    db.flush()
    return run


def test_artifact_storage_redaction_versioning_integrity_auth_and_retention(
    db_session: Session,
    tmp_path: Path,
) -> None:
    run = _seed_workflow(db_session)
    storage = LocalArtifactStorage(root_dir=tmp_path / "artifacts")
    service = ArtifactService(db_session, storage=storage)

    with pytest.raises(ValidationFailedError):
        storage.put("../escape.txt", b"evil")

    art_v1 = service.store_text_artifact(
        workflow_id=run.id,
        artifact_type="unified_diff",
        name="patch.diff",
        text_content="+TOKEN='ghp_1234567890abcdefghijklmnopqrstuv'\n",
        content_type="text/plain",
        created_by="admin",
        retention_days=7,
    )
    assert art_v1.version == 1

    art_v1_repeat = service.store_text_artifact(
        workflow_id=run.id,
        artifact_type="unified_diff",
        name="patch.diff",
        text_content="+TOKEN='ghp_1234567890abcdefghijklmnopqrstuv'\n",
        content_type="text/plain",
        created_by="admin",
    )
    assert art_v1_repeat.id == art_v1.id
    assert art_v1_repeat.version == 1

    meta, raw_bytes = service.read_artifact_bytes(art_v1.id, actor="admin")
    assert meta.checksum_sha256 == art_v1.checksum_sha256
    assert b"ghp_1234567890abcdefghijklmnopqrstuv" not in raw_bytes
    assert b"[REDACTED]" in raw_bytes

    with pytest.raises(AuthorizationError):
        service.read_artifact_bytes(art_v1.id, actor="other-user")

    art_v2 = service.store_json_artifact(
        workflow_id=run.id,
        artifact_type="unified_diff",
        name="patch.json",
        payload={"api_key": "sk-secret1234567890abcdefghij", "files": ["app/main.py"]},
        created_by="admin",
        retention_days=1,
    )
    assert art_v2.version == 2
    _m2, json_bytes = service.read_artifact_bytes(art_v2.id, actor="admin")
    parsed_json = json.loads(json_bytes.decode("utf-8"))
    assert parsed_json["api_key"] == "[REDACTED]"

    tampered_file = storage.root_dir / art_v1.storage_key
    tampered_file.write_bytes(b"corrupted content")
    with pytest.raises(ConflictError):
        service.read_artifact_bytes(art_v1.id, actor="admin")

    art_v2.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.flush()
    purged_count = service.purge_expired_artifacts()
    assert purged_count == 1
    assert not storage.exists(art_v2.storage_key)
    assert service.count_workflow_artifacts(run.id) == 1


def test_github_client_idempotent_pr_creation_and_ci_review_status() -> None:
    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        GITHUB_AUTH_MODE="pat_fallback",
        GITHUB_PAT_FALLBACK_TOKEN="ghp_testtoken1234567890abcdefghij",  # noqa: S106
    )

    created_prs: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/acme/orders/pulls":
            return httpx.Response(200, json=created_prs)
        if request.method == "POST" and path == "/repos/acme/orders/pulls":
            body = json.loads(request.content.decode("utf-8"))
            pr_obj = {
                "number": 42,
                "title": body["title"],
                "body": body["body"],
                "state": "open",
                "draft": body["draft"],
                "html_url": "https://github.com/acme/orders/pull/42",
                "head": {"ref": body["head"], "sha": "b" * 40},
                "base": {"ref": body["base"], "sha": "a" * 40},
                "merged": False,
            }
            created_prs.append(pr_obj)
            return httpx.Response(201, json=pr_obj)
        if request.method == "GET" and path == "/repos/acme/orders/pulls/42":
            return httpx.Response(200, json=created_prs[0])
        if request.method == "GET" and path == "/repos/acme/orders/pulls/42/reviews":
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "alice"},
                        "state": "APPROVED",
                        "submitted_at": "2026-11-26T00:10:00Z",
                    }
                ],
            )
        if request.method == "GET" and path == f"/repos/acme/orders/commits/{'b' * 40}/check-runs":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "success",
                            "details_url": "https://github.com/acme/orders/runs/1",
                        }
                    ]
                },
            )
        if request.method == "GET" and path == f"/repos/acme/orders/commits/{'b' * 40}/status":
            return httpx.Response(200, json={"state": "success"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as mock_http:
        client = GitHubClient(settings=settings, http_client=mock_http)

        assert not hasattr(client, "merge_pull_request")
        assert not hasattr(client, "deploy")

        pr1, reused1 = client.create_pull_request(
            "acme/orders",
            title="feat(items): add pagination",
            body="Adds limit and offset.",
            head_branch="ai-sdlc/wf-1",
            base_branch="main",
        )
        assert pr1.number == 42
        assert reused1 is False

        pr2, reused2 = client.create_pull_request(
            "acme/orders",
            title="feat(items): add pagination",
            body="Duplicate retry.",
            head_branch="ai-sdlc/wf-1",
            base_branch="main",
        )
        assert pr2.number == 42
        assert reused2 is True
        assert len(created_prs) == 1

        snapshot = client.get_pull_request_status("acme/orders", 42)
        assert snapshot.ci_status == "success"
        assert snapshot.review_decision == "approved"
        assert len(snapshot.checks) == 1
        assert len(snapshot.reviews) == 1


def test_workspace_service_commit_and_non_force_push_with_conflict_detection(
    tmp_path: Path,
) -> None:
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
        base_sha = str(seed_repo.index.commit("Initial seed").hexsha)
        default_branch = str(seed_repo.active_branch.name)
        seed_repo.create_remote("origin", str(bare_remote))
        seed_repo.git.push("origin", f"HEAD:refs/heads/{default_branch}")
    finally:
        seed_repo.close()

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        WORKSPACE_ROOT_DIR=tmp_path / "managed_ws",
    )
    ws_service = WorkspaceService(settings=settings)
    ws_info = ws_service.prepare_workspace(
        workspace_id="wf_delivery_01",
        clone_source=str(bare_remote),
        base_commit_sha=base_sha,
        branch_name=default_branch,
    )
    ws_path = Path(ws_info.workspace_path)

    readme_path = ws_path / "README.md"
    readme_path.write_text(
        readme_path.read_text(encoding="utf-8") + "\nUpdated pagination docs.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationFailedError):
        ws_service.create_branch_commit_and_push(
            workspace_id="wf_delivery_01",
            branch_name=default_branch,
            base_branch=default_branch,
            expected_base_sha=base_sha,
            commit_message="Should fail",
            remote_push_url=str(bare_remote),
        )

    res1 = ws_service.create_branch_commit_and_push(
        workspace_id="wf_delivery_01",
        branch_name="ai-sdlc/pagination-001",
        base_branch=default_branch,
        expected_base_sha=base_sha,
        commit_message="docs(items): add pagination note",
        remote_push_url=str(bare_remote),
    )
    assert res1.pushed is True
    assert res1.reused_existing_commit is False
    assert res1.commit_sha != base_sha

    res2 = ws_service.create_branch_commit_and_push(
        workspace_id="wf_delivery_01",
        branch_name="ai-sdlc/pagination-001",
        base_branch=default_branch,
        expected_base_sha=base_sha,
        commit_message="docs(items): add pagination note",
        remote_push_url=str(bare_remote),
    )
    assert res2.pushed is True
    assert res2.reused_existing_commit is True
    assert res2.commit_sha == res1.commit_sha

    ws_info_stale = ws_service.prepare_workspace(
        workspace_id="wf_delivery_02",
        clone_source=str(bare_remote),
        base_commit_sha=base_sha,
        branch_name=default_branch,
    )
    (Path(ws_info_stale.workspace_path) / "README.md").write_text("changed\n", encoding="utf-8")

    seed_repo2 = Repo(str(seed_dir))
    try:
        (seed_dir / "OTHER.md").write_text("concurrent change\n", encoding="utf-8")
        seed_repo2.git.add(A=True)
        seed_repo2.index.commit("Advance base branch")
        seed_repo2.git.push("origin", f"HEAD:refs/heads/{default_branch}")
    finally:
        seed_repo2.close()

    with pytest.raises(ConflictError):
        ws_service.create_branch_commit_and_push(
            workspace_id="wf_delivery_02",
            branch_name="ai-sdlc/pagination-002",
            base_branch=default_branch,
            expected_base_sha=base_sha,
            commit_message="Stale base push must fail",
            remote_push_url=str(bare_remote),
        )