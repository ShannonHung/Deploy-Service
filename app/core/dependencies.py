"""
app/core/dependencies.py

FastAPI dependency factory for scope-based access control.

Usage in routes:
    @router.get("/deploy")
    def deploy(user: User = Depends(get_current_user(["deploy_api"]))):
        ...
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from app.core.exceptions import AuthException, ForbiddenException
from app.core.logging import RequestIdFilter
from app.core.security import decode_access_token
from app.domain.models import User

# The tokenUrl must match your actual POST /token route path.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")


def get_current_user(required_scopes: list[str] | None = None) -> Callable:
    """Dependency factory that validates JWT and enforces scope requirements.

    Args:
        required_scopes: List of scope strings the token must contain ALL of.
                         Pass an empty list or None to only validate the token.

    Returns:
        A FastAPI dependency callable that resolves to the authenticated ``User``.

    Raises:
        AuthException:   Token is missing, invalid, or expired.
        ForbiddenException: Token is valid but lacks a required scope.
    """
    required: list[str] = required_scopes or []

    async def _dependency(token: str = Depends(oauth2_scheme)) -> User:
        payload = decode_access_token(token)  # raises AuthException on failure

        account: str | None = payload.get("sub")
        scopes: list[str] = payload.get("scopes", [])

        if not account:
            raise AuthException(
                "Token is missing subject claim.",
            )

        missing = [s for s in required if s not in scopes]
        if missing:
            raise ForbiddenException(
                f"Token is missing required scopes: {missing}",
                detail={"required": required, "missing": missing},
            )

        RequestIdFilter.set_account(account)
        return User(account=account, scopes=scopes)

    return _dependency

from app.core.redis_client import RedisClient
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.inventory_repository import (
    HttpInventoryRepository,
    InventoryRepository,
)
from app.repositories.cluster_node_lookup_repository import (
    HttpClusterNodeLookupRepository,
    ClusterNodeLookupRepository,
)
from app.repositories.bastion_mapping_repository import (
    HttpBastionMappingRepository,
    BastionMappingRepository,
)
from app.repositories.trace_cache_repository import (
    RedisTraceCache,
    TraceCacheRepository,
)
from app.services.command_service import CommandService
from app.core.config import get_settings

async def get_command_state_repository() -> CommandStateRepository:
    redis = await RedisClient.get_client()
    return CommandStateRepository(redis)


async def get_inventory_repository() -> InventoryRepository:
    s = get_settings()
    return HttpInventoryRepository(
        base_url=s.INVENTORY_API_URL,
        token=s.INVENTORY_API_TOKEN,
        timeout_seconds=s.INVENTORY_API_TIMEOUT_SECONDS,
    )


async def get_cluster_node_lookup_repository() -> ClusterNodeLookupRepository:
    s = get_settings()
    return HttpClusterNodeLookupRepository(
        base_url=s.CLUSTER_API_URL,
        token=s.CLUSTER_API_TOKEN,
        timeout_seconds=s.CLUSTER_API_TIMEOUT_SECONDS,
    )


async def get_bastion_mapping_repository() -> BastionMappingRepository:
    s = get_settings()
    return HttpBastionMappingRepository(
        base_url=s.CLUSTER_API_URL,
        token=s.CLUSTER_API_TOKEN,
        timeout_seconds=s.CLUSTER_API_TIMEOUT_SECONDS,
    )


async def get_command_service(
    repo: CommandStateRepository = Depends(get_command_state_repository),
    inventory: InventoryRepository = Depends(get_inventory_repository),
    cluster_node_lookup_repo: ClusterNodeLookupRepository = Depends(get_cluster_node_lookup_repository),
    mapping_repo: BastionMappingRepository = Depends(get_bastion_mapping_repository),
) -> CommandService:
    return CommandService(repo, inventory, cluster_node_lookup_repo, mapping_repo)


async def get_trace_cache_repository() -> TraceCacheRepository:
    # Use the binary client: cache stores gzip-compressed bytes which the
    # default decode_responses=True client would fail to read.
    redis = await RedisClient.get_binary_client()
    return RedisTraceCache(redis)
