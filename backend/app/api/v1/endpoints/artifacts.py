"""Authenticated REST endpoints for workflow artifacts and evidence-based reports."""

from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from app.api.deps import (
    ArtifactStorageDep,
    CurrentOperator,
    DbSession,
    GitHubClientDep,
    WorkspaceServiceDep,
)
from app.core.exceptions import ResourceNotFoundError
from app.models.workflow import WorkflowRunModel
from app.schemas.delivery import ArtifactListResponse, ArtifactRead, WorkflowReportResponse
from app.services.artifact_storage import ArtifactService
from app.services.delivery_service import ReleaseDeliveryService

router = APIRouter(tags=["artifacts-and-reports"])


@router.get("/workflows/{workflow_id}/artifacts", response_model=ArtifactListResponse)
def list_workflow_artifacts(
    workflow_id: str,
    db: DbSession,
    artifact_storage: ArtifactStorageDep,
    _operator: CurrentOperator,
) -> ArtifactListResponse:
    """List all versioned artifacts persisted for a workflow."""
    run = db.scalar(select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_id))
    if run is None:
        raise ResourceNotFoundError("Workflow not found.", details={"workflow_id": workflow_id})

    service = ArtifactService(db, storage=artifact_storage)
    rows = service.list_workflow_artifacts(workflow_id)
    items = [ArtifactRead.model_validate(row) for row in rows]
    return ArtifactListResponse(workflow_id=workflow_id, items=items, total=len(items))


@router.get("/artifacts/{artifact_id}", response_model=None)
def get_artifact(
    artifact_id: str,
    db: DbSession,
    artifact_storage: ArtifactStorageDep,
    operator: CurrentOperator,
    download: Annotated[bool, Query()] = False,
) -> ArtifactRead | Response:
    """Return authorized artifact metadata or stream checksum-verified artifact bytes."""
    service = ArtifactService(db, storage=artifact_storage)
    if download:
        meta, raw_bytes = service.read_artifact_bytes(artifact_id, actor=operator.username)
        return Response(
            content=raw_bytes,
            media_type=meta.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{meta.name}"',
                "X-Artifact-SHA256": meta.checksum_sha256,
            },
        )

    meta = service.get_artifact_metadata(artifact_id, actor=operator.username)
    return ArtifactRead.model_validate(meta)


@router.get("/workflows/{workflow_id}/report", response_model=WorkflowReportResponse)
def get_workflow_report(
    workflow_id: str,
    db: DbSession,
    github_client: GitHubClientDep,
    workspace_service: WorkspaceServiceDep,
    artifact_storage: ArtifactStorageDep,
    operator: CurrentOperator,
) -> WorkflowReportResponse:
    """Return the evidence-based completion and audit report for a workflow."""
    service = ReleaseDeliveryService(
        db,
        github_client=github_client,
        workspace_service=workspace_service,
        artifact_service=ArtifactService(db, storage=artifact_storage),
    )
    return service.get_workflow_report(workflow_id, actor=operator.username)