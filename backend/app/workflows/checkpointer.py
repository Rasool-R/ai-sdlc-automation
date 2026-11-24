"""Synchronous LangGraph checkpointer factories for PostgreSQL and in-memory testing."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver


def normalize_postgres_conninfo(database_url: str) -> str:
    """Convert a SQLAlchemy PostgreSQL URL into a libpq/psycopg connection string."""
    cleaned = database_url.strip()
    for prefix in (
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        "postgresql+psycopg_async://",
    ):
        if cleaned.startswith(prefix):
            return "postgresql://" + cleaned[len(prefix) :]
    return cleaned


@contextmanager
def postgres_checkpointer(
    database_url: str,
    *,
    auto_setup: bool = False,
) -> Generator[Any, None, None]:
    """Yield a synchronous PostgreSQL LangGraph checkpointer safe for Celery workers."""
    conninfo = normalize_postgres_conninfo(database_url)
    with PostgresSaver.from_conn_string(conninfo) as saver:
        if auto_setup:
            saver.setup()
        yield saver


def bootstrap_postgres_checkpointer(database_url: str) -> None:
    """Create LangGraph's own checkpoint tables in PostgreSQL (idempotent bootstrap step)."""
    with postgres_checkpointer(database_url, auto_setup=True):
        return


def create_memory_checkpointer() -> Any:
    """Create an in-memory LangGraph checkpointer for isolated unit tests."""
    return MemorySaver()