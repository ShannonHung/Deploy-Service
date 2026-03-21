import logging
from fastapi import APIRouter, Depends, Request, HTTPException

from app.api.v1.schemas.command import CommandExecutionRequest, CommandExecutionResponse, UserCommandWhitelist, CommandWhitelistConfig
from app.services.command_service import CommandService, CommandExecutionException
from app.core.dependencies import get_current_user
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
) -> ApiResponse[UserCommandWhitelist]:
    try:
        whitelist = CommandService.get_user_commands(current_user.account)
        return ApiResponse(data=whitelist, request_id=_request_id(request))
    except CommandExecutionException as e:
        raise HTTPException(status_code=403, detail=str(e))

@router.get(
    "/{command_name}/info",
    response_model=ApiResponse[CommandWhitelistConfig],
    summary="Get Specific Command Info",
)
async def get_specific_command_info(
    command_name: str,
    request: Request,
    current_user: User = Depends(get_current_user(["command_api"])),
) -> ApiResponse[CommandWhitelistConfig]:
    try:
        cmd_info = CommandService.get_command_info(current_user.account, command_name)
        return ApiResponse(data=cmd_info, request_id=_request_id(request))
    except CommandExecutionException as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post(
    "/execution",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Execute SSH Command Pipeline",
)
async def execute_command_endpoint(
    request: Request,
    body: CommandExecutionRequest,
    current_user: User = Depends(get_current_user(["command_api"])),
) -> ApiResponse[CommandExecutionResponse]:
    req_id = _request_id(request)
    response_data = await CommandService.execute_command(current_user.account, req_id, body)
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
) -> ApiResponse[CommandExecutionResponse]:
    try:
        response_data = CommandService.get_command_execution_result(command_id)
        return ApiResponse(data=response_data, request_id=_request_id(request))
    except CommandExecutionException as e:
        raise HTTPException(status_code=404, detail=str(e))
