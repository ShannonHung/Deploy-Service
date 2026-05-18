import logging
from fastapi import APIRouter, Depends, Request

from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    CommandStatus,
    UserCommandWhitelist, CommandWhitelistConfig,
)
from app.services.command_service import CommandService
from app.core.dependencies import get_current_user, get_command_service
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
