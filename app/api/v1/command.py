import logging
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse

from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    CommandStatus, CommandTraceResponse,
    UserCommandWhitelist, CommandWhitelistConfig,
)
from app.core.log_viewer_template import LOG_VIEWER_HTML
from app.services.command_service import CommandService
from app.core.dependencies import (
    get_current_user, get_current_user_cookie_or_header, get_command_service,
)
from app.core.exceptions import (
    CommandExecutionException, ConflictException, NotFoundException,
)
from app.domain.models import User, ApiResponse

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/command", tags=["command"])


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@router.get(
    "/info",
    response_model=ApiResponse[UserCommandWhitelist],
    summary="Get All Available Commands",
)
async def get_all_commands_info(
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[UserCommandWhitelist]:
    whitelist = svc.get_user_commands(current_user.account)
    return ApiResponse(data=whitelist, request_id=_request_id(request))


@router.get(
    "/{command_name}/info",
    response_model=ApiResponse[CommandWhitelistConfig],
    summary="Get Specific Command Info",
)
async def get_specific_command_info(
    command_name: str,
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandWhitelistConfig]:
    cmd_info = svc.get_command_info(current_user.account, command_name)
    return ApiResponse(data=cmd_info, request_id=_request_id(request))


@router.post(
    "/execution",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Execute SSH Command Pipeline",
)
async def execute_command_endpoint(
    request: Request,
    body: CommandExecutionRequest,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandExecutionResponse]:
    req_id = _request_id(request)
    response_data = await svc.execute_command(current_user.account, req_id, body)
    return ApiResponse(data=response_data, request_id=req_id)


@router.get(
    "/execution/{command_id}",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Poll Command Execution Result",
)
async def get_command_execution_status(
    command_id: str,
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandExecutionResponse]:
    response_data = await svc.get_command_execution_result(command_id)
    return ApiResponse(data=response_data, request_id=_request_id(request))


@router.get(
    "/execution/{command_id}/trace/ui",
    response_model=ApiResponse[CommandTraceResponse],
    summary="Get formatted command logs for UI",
    description="Incremental tail of the control_node run log; poll with byte_offset.",
)
async def get_command_trace_ui(
    command_id: str,
    request: Request,
    byte_offset: int = Query(0, ge=0),
    line_num: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user_cookie_or_header(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandTraceResponse]:
    data = await svc.get_command_trace(command_id, byte_offset, line_num)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/execution/{command_id}/view",
    response_class=HTMLResponse,
    summary="View command logs in UI",
    description="Auto-refreshing log viewer for a long-running command.",
)
async def view_command(command_id: str):
    # Mirror deploy's view_job auth posture: the HTML shell is unauthed; the
    # /trace/ui endpoint it polls carries its own command_api-scoped token.
    trace_url = f"/api/v1/command/execution/{command_id}/trace/ui"
    meta_html = f'<div><span class="label">Command ID</span><code>{command_id}</code></div>'
    return LOG_VIEWER_HTML.format(
        title=f"Command Log Viewer | {command_id}",
        heading=f"Command: {command_id}",
        trace_url=trace_url,
        terminal_statuses_json="['success','failed','killed']",
        meta_html=meta_html,
    )


@router.post(
    "/execution/{command_id}/kill",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Kill Running Command",
)
async def kill_command_endpoint(
    command_id: str,
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
    svc: CommandService = Depends(get_command_service),
) -> ApiResponse[CommandExecutionResponse]:
    req_id = _request_id(request)
    try:
        state = await svc.repo.get(command_id)
    except CommandExecutionException as exc:
        raise NotFoundException(
            f"Command {command_id} not found.", detail={"command_id": command_id}
        ) from exc

    if state.status != CommandStatus.RUNNING:
        raise ConflictException(
            f"Cannot kill command in {state.status} state.",
            detail={"command_id": command_id, "current_status": state.status},
        )

    await svc.kill_command(command_id, message="Killed by user request.")

    return ApiResponse(
        data=CommandExecutionResponse(
            command_id=command_id,
            status="accepted",
            message="Kill request accepted",
        ),
        request_id=req_id,
    )
