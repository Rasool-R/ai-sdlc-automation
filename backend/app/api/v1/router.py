"""Primary API v1 router aggregating all versioned endpoint modules."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    artifacts,
    auth,
    health,
    projects,
    pull_requests,
    repositories,
    tasks,
    workflows,
)

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(repositories.router)
api_v1_router.include_router(workflows.router)
api_v1_router.include_router(tasks.router)
api_v1_router.include_router(pull_requests.router)
api_v1_router.include_router(artifacts.router)