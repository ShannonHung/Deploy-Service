"""Inventory API proxy endpoints.

GET /api/v1/inventory/nodes/{node_name}  → ClusterNodeInfo
GET /api/v1/inventory/mappings           → list[BastionMapping]  (?type=<type>)

All endpoints require command_api scope.
"""
from __future__ import annotations

from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.core.dependencies import (
    get_current_user,
    get_inventory_repository,
    get_inventory_service,
)
from app.domain.models import ApiResponse, User
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    InventoryRepository,
    NodeBastionResolution,
)
from app.services.inventory_service import InventoryService

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@router.get(
    "/nodes/{node_name}",
    response_model=ApiResponse[ClusterNodeInfo],
    summary="Look up cluster node info by node name",
)
async def get_node(
    request: Request,
    node_name: str,
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
    repo: InventoryRepository = Depends(get_inventory_repository),
) -> ApiResponse[ClusterNodeInfo]:
    data = await repo.lookup_by_name(node_name)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/mappings",
    response_model=ApiResponse[List[BastionMapping]],
    summary="List bastion-cluster mappings by type",
)
async def get_mappings(
    request: Request,
    type: str = Query(..., description="Bastion type name"),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
    repo: InventoryRepository = Depends(get_inventory_repository),
) -> ApiResponse[List[BastionMapping]]:
    data = await repo.list_mappings(type)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/nodes/{node_name}/bastion-resolution",
    response_model=ApiResponse[NodeBastionResolution],
    summary="Resolve node name to bastion runner",
)
async def get_node_bastion_resolution(
    request: Request,
    node_name: str,
    bastion_type: Optional[str] = Query(default=None, description="Override bastion type (default: derived from node_type via BASTION_NODE_TYPE_MAP config)"),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
    service: InventoryService = Depends(get_inventory_service),
) -> ApiResponse[NodeBastionResolution]:
    data = await service.resolve_node_bastion(node_name, bastion_type_override=bastion_type)
    return ApiResponse(data=data, request_id=_request_id(request))
