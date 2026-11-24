"""Opt-in LangSmith tracing policy and secret-safe metadata construction."""

from typing import Any

from app.core.config import Settings
from app.core.logging import redact_data


def should_enable_langsmith_tracing(
    *,
    settings: Settings,
    is_private_repo: bool,
    llm_data_sharing: bool,
) -> bool:
    """Enable LangSmith tracing only when configured, credentialed, and permitted by privacy."""
    if not settings.LANGSMITH_TRACING:
        return False
    if not settings.LANGSMITH_API_KEY.strip():
        return False
    return not (is_private_repo and not llm_data_sharing)


def build_safe_trace_metadata(
    *,
    workflow_id: str,
    repository_id: str,
    base_commit_sha: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build secret-redacted metadata safe for LangGraph and LangSmith run traces."""
    raw: dict[str, Any] = {
        "workflow_id": workflow_id,
        "repository_id": repository_id,
        "base_commit_sha": base_commit_sha,
    }
    if extra:
        raw.update(extra)
    redacted = redact_data(raw)
    return redacted if isinstance(redacted, dict) else {}


def build_langgraph_run_config(
    *,
    thread_id: str,
    workflow_id: str,
    repository_id: str,
    base_commit_sha: str,
    settings: Settings,
    is_private_repo: bool,
    llm_data_sharing: bool,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return (langgraph_config, tracing_enabled) enforcing private-repository opt-in."""
    tracing_enabled = should_enable_langsmith_tracing(
        settings=settings,
        is_private_repo=is_private_repo,
        llm_data_sharing=llm_data_sharing,
    )
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
    }
    if tracing_enabled:
        config["run_name"] = f"ai-sdlc:{workflow_id}"
        config["tags"] = [
            f"project:{settings.LANGSMITH_PROJECT}",
            f"env:{settings.APP_ENV}",
        ]
        config["metadata"] = build_safe_trace_metadata(
            workflow_id=workflow_id,
            repository_id=repository_id,
            base_commit_sha=base_commit_sha,
            extra=extra_metadata,
        )
    return config, tracing_enabled