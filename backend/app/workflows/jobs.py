"""Background worker health and diagnostic jobs executed by Celery workers."""

from typing import Any

from app.workflows.celery_app import celery_app


def execute_worker_ping() -> dict[str, str]:
    """Return a deterministic pong payload to verify worker execution."""
    return {"status": "pong"}


ping_worker: Any = celery_app.task(name="workflows.ping")(execute_worker_ping)