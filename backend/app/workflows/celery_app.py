"""Celery application factory configured for reliable background job execution."""

from celery import Celery # type: ignore[import-untyped]

from app.core.config import get_settings


def create_celery_app() -> Celery:
    """Create and configure the Celery application using validated settings."""
    settings = get_settings()
    app = Celery(
        settings.APP_NAME,
        broker=settings.CELERY_BROKER_URL,
        backend=settings.CELERY_RESULT_BACKEND,
        include=["app.workflows.jobs"],
    )
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
    )
    return app


celery_app = create_celery_app()