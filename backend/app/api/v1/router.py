"""Primary API v1 router aggregating all versioned endpoint modules."""

from fastapi import APIRouter

from app.api.v1.endpoints import auth, health, projects, repositories

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(repositories.router)