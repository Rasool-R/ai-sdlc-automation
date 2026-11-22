"""Unit tests for persistence models, unique constraints, audit redaction, and repo locks."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.exceptions import ConflictError
from app.core.logging import REDACTED_PLACEHOLDER, correlation_id_ctx
from app.models import (
    Base,
    ProjectModel,
    RepositoryModel,
    RepositoryVersionModel,
    WorkflowRunModel,
)
from app.services.audit_service import AuditService
from app.services.lock_service import RepositoryLockService
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _create_project_and_repo(db: Session) -> tuple[ProjectModel, RepositoryModel]:
    project = ProjectModel(name="demo-platform", description="Core API project")
    db.add(project)
    db.flush()

    repo = RepositoryModel(
        project_id=project.id,
        name="demo-service",
        full_name="org/demo-service",
        provider="github",
        clone_url="https://github.com/org/demo-service.git",
        default_branch="main",
        is_private=True,
        llm_data_sharing=False,
    )
    db.add(repo)
    db.flush()
    return project, repo


def test_repository_version_and_workflow_idempotency_constraints(db_session: Session) -> None:
    _project, repo = _create_project_and_repo(db_session)

    version = RepositoryVersionModel(
        repository_id=repo.id,
        branch_name="main",
        commit_sha="a" * 40,
        commit_message="Initial commit",
        author_name="dev",
        committed_at=datetime.now(UTC),
    )
    db_session.add(version)
    db_session.flush()

    duplicate_version = RepositoryVersionModel(
        repository_id=repo.id,
        branch_name="main",
        commit_sha="a" * 40,
    )
    db_session.add(duplicate_version)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    _project, repo = _create_project_and_repo(db_session)
    wf1 = WorkflowRunModel(
        repository_id=repo.id,
        base_branch="main",
        base_commit_sha="b" * 40,
        thread_id="thread-001",
        idempotency_key="idem-key-001",
        title="Add pagination",
        request_text="Add limit/offset pagination to list endpoint.",
        cost_limit_usd=Decimal("5.0000"),
        created_by="admin",
    )
    db_session.add(wf1)
    db_session.flush()

    wf_dup = WorkflowRunModel(
        repository_id=repo.id,
        base_branch="main",
        base_commit_sha="b" * 40,
        thread_id="thread-002",
        idempotency_key="idem-key-001",
        title="Duplicate workflow",
        request_text="Duplicate submission.",
        cost_limit_usd=Decimal("5.0000"),
        created_by="admin",
    )
    db_session.add(wf_dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_audit_service_redacts_secrets_and_captures_correlation_id(db_session: Session) -> None:
    audit_service = AuditService(db_session)
    token = correlation_id_ctx.set("corr-audit-999")
    try:
        entry = audit_service.record(
            actor="admin",
            action="repository.connect",
            resource_type="repository",
            resource_id="repo-123",
            details={
                "full_name": "org/private-repo",
                "api_key": "sk-super-secret-value-1234567890",
                "notes": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
            },
        )
    finally:
        correlation_id_ctx.reset(token)

    assert entry.correlation_id == "corr-audit-999"
    assert entry.details["full_name"] == "org/private-repo"
    assert entry.details["api_key"] == REDACTED_PLACEHOLDER
    assert "eyJhbGciOiJIUzI1NiJ9" not in entry.details["notes"]


def test_repository_lock_acquire_contention_expiry_and_release(db_session: Session) -> None:
    _project, repo = _create_project_and_repo(db_session)
    lock_service = RepositoryLockService(db_session)

    assert lock_service.is_locked(repository_id=repo.id) is False

    lock = lock_service.acquire(
        repository_id=repo.id,
        owner_id="wf-1",
        operation="repo.inspect",
        ttl_seconds=300,
    )
    assert lock.owner_id == "wf-1"
    assert lock_service.is_locked(repository_id=repo.id) is True

    # Re-acquiring by the same owner extends lock cleanly
    renewed = lock_service.acquire(
        repository_id=repo.id,
        owner_id="wf-1",
        operation="repo.inspect",
        ttl_seconds=600,
    )
    assert renewed.owner_id == "wf-1"

    # Different owner must fail with ConflictError while lock is active
    with pytest.raises(ConflictError):
        lock_service.acquire(
            repository_id=repo.id,
            owner_id="wf-2",
            operation="repo.patch",
        )

    # Releasing by wrong owner must fail with ConflictError
    with pytest.raises(ConflictError):
        lock_service.release(repository_id=repo.id, owner_id="wf-2")

    # Expired lock can be reclaimed by a new owner
    renewed.expires_at = datetime.now(UTC) - timedelta(seconds=10)
    db_session.flush()
    reclaimed = lock_service.acquire(
        repository_id=repo.id,
        owner_id="wf-2",
        operation="repo.patch",
        ttl_seconds=300,
    )
    assert reclaimed.owner_id == "wf-2"

    # Releasing by current owner succeeds
    assert lock_service.release(repository_id=repo.id, owner_id="wf-2") is True
    assert lock_service.is_locked(repository_id=repo.id) is False
    assert lock_service.release(repository_id=repo.id, owner_id="wf-2") is False