"""Unit tests for persisted workflow events, secret redaction, cursor pagination, and SSE."""

import json
from collections.abc import Generator
from decimal import Decimal

import pytest
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models import Base, ProjectModel, RepositoryModel, WorkflowEventModel, WorkflowRunModel
from app.services.event_service import WorkflowEventService
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def events_api_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, sessionmaker[Session], dict[str, str], str], None, None]:
    fernet_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-with-at-least-32-characters-666")
    monkeypatch.setenv("OPERATOR_USERNAME", "admin")
    monkeypatch.setenv("OPERATOR_PASSWORD_HASH", hash_password("EventsPass!2026"))
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
        project = ProjectModel(name="events-proj")
        seed_db.add(project)
        seed_db.flush()
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
        seed_db.add(repo)
        seed_db.flush()
        run = WorkflowRunModel(
            repository_id=repo.id,
            base_branch="main",
            base_commit_sha="a" * 40,
            thread_id="wf-evt-thread-1",
            idempotency_key="wf-evt-idem-1",
            title="Event stream test",
            request_text="Verify monotonic events and SSE.",
            status="running",
            current_node="coding",
            cost_limit_usd=Decimal("5.0000"),
            created_by="admin",
        )
        seed_db.add(run)
        seed_db.commit()
        wf_id = run.id

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

    with TestClient(app) as client:
        yield client, session_factory, headers, wf_id

    engine.dispose()
    get_settings.cache_clear()


def test_events_endpoint_requires_auth_and_returns_404_for_unknown_workflow(
    events_api_env: tuple[TestClient, sessionmaker[Session], dict[str, str], str],
) -> None:
    client, _sf, headers, wf_id = events_api_env
    assert client.get(f"/api/v1/workflows/{wf_id}/events").status_code == 401
    assert client.get(f"/api/v1/workflows/{wf_id}/events?stream=true").status_code == 401
    assert client.get("/api/v1/workflows/does-not-exist/events", headers=headers).status_code == 404
    assert (
        client.get(
            "/api/v1/workflows/does-not-exist/events?stream=true",
            headers=headers,
        ).status_code
        == 404
    )


def test_events_redaction_monotonic_cursor_pagination_and_idempotent_sync(
    events_api_env: tuple[TestClient, sessionmaker[Session], dict[str, str], str],
) -> None:
    client, session_factory, headers, wf_id = events_api_env

    with session_factory() as db:
        service = WorkflowEventService(db)
        e1 = service.record_event(
            workflow_id=wf_id,
            event_type="workflow.enqueued",
            status="pending",
            node_name="queued",
            message="Started with token ghp_1234567890abcdefghijklmnopqrstuv",
            payload={
                "api_key": "sk-secret1234567890abcdefghij",
                "raw_reasoning": "hidden internal chain of thought",
                "step": "init",
            },
        )
        assert "ghp_1234567890abcdefghijklmnopqrstuv" not in e1.message
        assert "[REDACTED]" in e1.message
        assert e1.payload["api_key"] == "[REDACTED]"
        assert "raw_reasoning" not in e1.payload
        assert e1.payload["step"] == "init"

        sample_state = {
            "base_commit_sha": "a" * 40,
            "node_history": ["requirement_analysis", "planning"],
            "implementation_plan": {"tasks": [{"task_id": "T-1"}, {"task_id": "T-2"}]},
            "plan_hash": "b" * 64,
            "llm_calls_count": 2,
            "total_tokens": 500,
            "estimated_cost_usd": "0.001000",
            "cost_status": "estimated",
        }
        interrupt_payload = {
            "type": "plan_approval_required",
            "base_commit_sha": "a" * 40,
            "plan_hash": "b" * 64,
            "expected_files": ["app/main.py"],
            "estimated_cost_usd": "0.001000",
            "cost_status": "estimated",
        }

        emitted_first = service.sync_transition_events(
            workflow_id=wf_id,
            status="waiting_plan_approval",
            current_node="human_plan_approval",
            state=sample_state,
            interrupt_payload=interrupt_payload,
        )
        assert len(emitted_first) == 3

        # Re-syncing identical state produces zero duplicate events
        emitted_repeat = service.sync_transition_events(
            workflow_id=wf_id,
            status="waiting_plan_approval",
            current_node="human_plan_approval",
            state=sample_state,
            interrupt_payload=interrupt_payload,
        )
        assert emitted_repeat == []
        db.commit()

    # Cursor pagination page 1 (limit=2)
    p1 = client.get(f"/api/v1/workflows/{wf_id}/events?limit=2", headers=headers)
    assert p1.status_code == 200
    body1 = p1.json()
    assert body1["total"] == 4
    assert body1["has_more"] is True
    assert len(body1["items"]) == 2
    assert body1["items"][0]["id"] < body1["items"][1]["id"]
    cursor1 = body1["next_cursor"]
    assert cursor1 == body1["items"][-1]["id"]

    # Cursor pagination page 2 (after_id=cursor1)
    p2 = client.get(
        f"/api/v1/workflows/{wf_id}/events?after_id={cursor1}&limit=10",
        headers=headers,
    )
    assert p2.status_code == 200
    body2 = p2.json()
    assert body2["has_more"] is False
    assert len(body2["items"]) == 2
    assert all(item["id"] > cursor1 for item in body2["items"])
    assert body2["items"][-1]["event_type"] == "human.action_required"


def test_sse_stream_orders_events_honors_last_event_id_and_closes_on_terminal_status(
    events_api_env: tuple[TestClient, sessionmaker[Session], dict[str, str], str],
) -> None:
    client, session_factory, headers, wf_id = events_api_env

    with session_factory() as db:
        service = WorkflowEventService(db)
        first = service.record_event(
            workflow_id=wf_id,
            event_type="node.executed",
            status="running",
            node_name="coding",
            message="Applied patch cleanly.",
            payload={"step_index": 0},
        )
        second = service.record_event(
            workflow_id=wf_id,
            event_type="workflow.completed",
            status="completed",
            node_name="release_preparation",
            message="Workflow completed.",
            payload={"pr_number": 42},
        )
        run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == wf_id))
        assert run is not None
        run.status = "completed"
        db.commit()
        first_id = first.id
        second_id = second.id

    # Full SSE stream via ?stream=true closes cleanly on terminal workflow
    with client.stream(
        "GET",
        f"/api/v1/workflows/{wf_id}/events?stream=true&poll_interval_seconds=0.05",
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        raw_text = "".join(resp.iter_text())

    assert f"id: {first_id}\nevent: node.executed\n" in raw_text
    assert f"id: {second_id}\nevent: workflow.completed\n" in raw_text
    assert ": workflow_terminal:completed" in raw_text

    # Reconnecting with Last-Event-ID header and Accept: text/event-stream skips earlier events
    sse_headers = {
        **headers,
        "Accept": "text/event-stream",
        "Last-Event-ID": str(first_id),
    }
    with client.stream(
        "GET",
        f"/api/v1/workflows/{wf_id}/events?follow=false",
        headers=sse_headers,
    ) as replay_resp:
        assert replay_resp.status_code == 200
        replay_text = "".join(replay_resp.iter_text())

    assert f"id: {first_id}\n" not in replay_text
    assert f"id: {second_id}\nevent: workflow.completed\n" in replay_text
    data_lines = [line[6:] for line in replay_text.splitlines() if line.startswith("data: ")]
    assert len(data_lines) == 1
    parsed = json.loads(data_lines[0])
    assert parsed["id"] == second_id
    assert parsed["payload"]["pr_number"] == 42

    with session_factory() as db:
        all_rows = list(db.scalars(select(WorkflowEventModel)).all())
    assert len(all_rows) == 2