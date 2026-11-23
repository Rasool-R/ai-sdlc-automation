"""FastAPI application exposing the narrow internal sandbox-runner API."""

import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from sandbox_runner.config import SandboxRunnerSettings, get_sandbox_settings
from sandbox_runner.manager import (
    SandboxNotFoundError,
    SandboxRunManager,
    SandboxValidationError,
)
from sandbox_runner.runtime import ContainerRuntime, DockerCliRuntime
from sandbox_runner.schemas import (
    CommandExecutionRecord,
    CreateRunRequest,
    CreateRunResponse,
    DestroyRunResponse,
    ExecuteCommandRequest,
    RunResultsResponse,
    UploadWorkspaceResponse,
)


def create_sandbox_app(
    *,
    settings: SandboxRunnerSettings | None = None,
    runtime: ContainerRuntime | None = None,
) -> FastAPI:
    """Create and configure the isolated sandbox-runner FastAPI service."""
    resolved_settings = settings or get_sandbox_settings()
    resolved_runtime = runtime or DockerCliRuntime(resolved_settings)
    manager = SandboxRunManager(settings=resolved_settings, runtime=resolved_runtime)

    app = FastAPI(
        title="AI SDLC Sandbox Runner",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    def verify_internal_token(
        x_sandbox_token: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = resolved_settings.SANDBOX_INTERNAL_TOKEN.strip()
        if not expected:
            return
        if not x_sandbox_token or not hmac.compare_digest(x_sandbox_token, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing internal sandbox token.",
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "sandbox-runner"}

    @app.post(
        "/runs",
        response_model=CreateRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_run(
        payload: CreateRunRequest,
        _auth: Annotated[None, Depends(verify_internal_token)],
    ) -> CreateRunResponse:
        try:
            return manager.create_run(payload)
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    @app.post("/runs/{run_id}/workspace", response_model=UploadWorkspaceResponse)
    async def upload_workspace(
        run_id: str,
        request: Request,
        _auth: Annotated[None, Depends(verify_internal_token)],
    ) -> UploadWorkspaceResponse:
        archive_bytes = await request.body()
        try:
            return manager.upload_workspace_archive(run_id, archive_bytes)
        except SandboxNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    @app.post("/runs/{run_id}/execute", response_model=CommandExecutionRecord)
    def execute_command(
        run_id: str,
        payload: ExecuteCommandRequest,
        _auth: Annotated[None, Depends(verify_internal_token)],
    ) -> CommandExecutionRecord:
        try:
            return manager.execute_command(run_id, payload)
        except SandboxNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    @app.get("/runs/{run_id}/results", response_model=RunResultsResponse)
    def get_results(
        run_id: str,
        _auth: Annotated[None, Depends(verify_internal_token)],
    ) -> RunResultsResponse:
        try:
            return manager.get_results(run_id)
        except SandboxNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.delete("/runs/{run_id}", response_model=DestroyRunResponse)
    def destroy_run(
        run_id: str,
        _auth: Annotated[None, Depends(verify_internal_token)],
    ) -> DestroyRunResponse:
        return manager.destroy_run(run_id)

    return app


app = create_sandbox_app()