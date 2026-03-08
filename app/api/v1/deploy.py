"""
app/api/v1/deploy.py

GitLab pipeline deployment endpoints (v1).
All endpoints require the ``deploy_api`` scope.

Route layout:
  POST /api/v1/deploy/stage                      → trigger pipeline
  GET  /api/v1/deploy/stage/{pipeline_id}        → get pipeline status
  POST /api/v1/deploy/stage/{pipeline_id}/cancel → cancel pipeline
  POST /api/v1/deploy/stage/{pipeline_id}/retry  → retry pipeline
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.core.log_viewer_template import LOG_VIEWER_HTML
from app.domain.models import ApiResponse, User
from app.domain.pipeline_models import (
    CancelRetryData,
    PipelineData,
    RunningPipelinesData,
    TriggerPipelineRequest,
    FormattedLogResponse,
)
from app.repositories.gitlab_pipeline_repository import GitlabPipelineRepository
from app.services.deploy_service import DeployService

_logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/deploy",
    tags=["deploy"],
)


def _get_deploy_service() -> DeployService:
    """Build DeployService backed by a live GitLab client."""
    settings = get_settings()
    repo = GitlabPipelineRepository(
        url=settings.GITLAB_URL,
        token=settings.GITLAB_TOKEN,
        project_id=settings.GITLAB_PROJECT_ID,
    )
    return DeployService(repo)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


# ── POST /api/v1/deploy/stage ─────────────────────────────────────────────────

@router.post(
    "/stage",
    response_model=ApiResponse[PipelineData],
    summary="Trigger a GitLab pipeline",
    description=(
        "Triggers a new GitLab pipeline on the specified ref. "
        "The `action` query param is forwarded as the `EXECUTION` pipeline variable. "
        "Body variables are merged in (EXECUTION always wins if duplicated)."
    ),
)
async def trigger_pipeline(
    request: Request,
    action: str = Query(..., description="Pipeline EXECUTION variable value (e.g. test-deploy)"),
    ref_name: str = Query(default="main", description="Git branch or tag to run pipeline on"),
    body: TriggerPipelineRequest = TriggerPipelineRequest(),
    svc: DeployService = Depends(_get_deploy_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.trigger_pipeline(
        action=action,
        ref=ref_name,
        extra_variables=body.variables,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/stage/check-running ─────────────────────────────────

@router.post(
    "/stage/check-running",
    response_model=ApiResponse[RunningPipelinesData],
    summary="Check for duplicate running pipelines",
    description=(
        "Returns all active (created / pending / running / …) pipelines on *ref_name* "
        "whose variables exactly match *action* + body variables. "
        "Use this before triggering to preview what would be blocked. "
        "The trigger endpoint performs this check automatically."
    ),
)
async def check_running(
    request: Request,
    action: str = Query(..., description="EXECUTION variable value to match"),
    ref_name: str = Query(default="main", description="Branch or tag to filter on"),
    body: TriggerPipelineRequest = TriggerPipelineRequest(),
    svc: DeployService = Depends(_get_deploy_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[RunningPipelinesData]:
    data = await svc.find_duplicate_pipelines(
        action=action,
        ref=ref_name,
        extra_variables=body.variables,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── GET /api/v1/deploy/stage/{pipeline_id} ───────────────────────────────────

@router.get(
    "/stage/{pipeline_id}",
    response_model=ApiResponse[PipelineData],
    summary="Get pipeline status",
    description="Returns the current state of an existing pipeline.",
)
async def get_pipeline(
    request: Request,
    pipeline_id: int,
    svc: DeployService = Depends(_get_deploy_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.get_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/stage/{pipeline_id}/cancel ───────────────────────────

@router.post(
    "/stage/{pipeline_id}/cancel",
    response_model=ApiResponse[PipelineData],
    summary="Cancel a pipeline",
    description="Cancels a running pipeline and returns its updated status.",
)
async def cancel_pipeline(
    request: Request,
    pipeline_id: int,
    svc: DeployService = Depends(_get_deploy_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.cancel_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/stage/{pipeline_id}/retry ────────────────────────────

@router.post(
    "/stage/{pipeline_id}/retry",
    response_model=ApiResponse[PipelineData],
    summary="Retry a pipeline",
    description="Retries a failed or cancelled pipeline and returns the new state.",
)
async def retry_pipeline(
    request: Request,
    pipeline_id: int,
    svc: DeployService = Depends(_get_deploy_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.retry_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── GET /api/v1/deploy/jobs/{job_id}/trace ───────────────────────────────────

@router.get(
    "/jobs/{job_id}/trace",
    summary="Get job console logs",
    description="Returns the raw console output for a specific job ID.",
)
async def get_job_trace(
    job_id: int,
    svc: DeployService = Depends(_get_deploy_service),
) -> str:
    """Returns raw text trace directly."""
    status, trace = await svc.get_job_trace(job_id)
    return trace


@router.get(
    "/jobs/{job_id}/trace/ui",
    response_model=ApiResponse[FormattedLogResponse],
    summary="Get formatted job logs for UI",
    description="Returns processed HTML lines with timestamps and section markers.",
)
async def get_formatted_job_trace(
    request: Request,
    job_id: int,
    offset: int = 0,
    svc: DeployService = Depends(_get_deploy_service),
) -> ApiResponse[FormattedLogResponse]:
    data = await svc.get_formatted_job_trace(job_id, offset)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/jobs/{job_id}/view",
    response_class=HTMLResponse,
    summary="View job logs in UI",
    description="Opens a beautiful, auto-refreshing log viewer for the specific job.",
)
async def view_job(job_id: int):
    """Returns a styled HTML log viewer."""
    return LOG_VIEWER_HTML.format(job_id=job_id)
