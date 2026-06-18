"""Host resolver strategy: chooses the SSH target IP based on host_type.

Adding a new host type:
  1. Add a value to HostType in app/domain/command.py.
  2. Add a HostResolver subclass here.
  3. Add a branch to create_host_resolver().
CommandService does not need to change.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Dict, Optional

from pydantic import BaseModel, Field

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.domain.command import HostType
from app.repositories.inventory_repository import (
    BastionMappingRepository,
    ClusterNodeLookupRepository,
    InventoryRepository,
)

_logger = logging.getLogger(__name__)


class ResolvedHost(BaseModel):
    ip: str
    source_input: str
    metadata: Dict[str, str] = Field(default_factory=dict)


class HostResolver(ABC):
    @abstractmethod
    async def resolve(self, raw_host: str) -> ResolvedHost: ...


class IpHostResolver(HostResolver):
    async def resolve(self, raw_host: str) -> ResolvedHost:
        return ResolvedHost(ip=raw_host, source_input=raw_host)


class HostnameHostResolver(HostResolver):
    def __init__(self, inventory: InventoryRepository) -> None:
        self._inventory = inventory

    async def resolve(self, raw_host: str) -> ResolvedHost:
        info = await self._inventory.lookup(raw_host)
        return ResolvedHost(
            ip=info.ip,
            source_input=raw_host,
            metadata={"hostname": info.hostname},
        )


class ClusterBastionHostResolver(HostResolver):
    """Resolve node_name → cluster_name → bastion_ip via two API calls.

    bastion_type is derived from node_type via node_type_map unless
    bastion_type_override is given (e.g. from request option).
    """

    def __init__(
        self,
        cluster_node_lookup_repo: ClusterNodeLookupRepository,
        mapping_repo: BastionMappingRepository,
        node_type_map: Dict[str, str],
        bastion_type: Optional[str] = None,
    ) -> None:
        self._cluster_node_lookup_repo = cluster_node_lookup_repo
        self._mapping_repo = mapping_repo
        self._node_type_map = node_type_map
        self._bastion_type = bastion_type

    async def resolve(self, raw_host: str) -> ResolvedHost:
        node_info = await self._cluster_node_lookup_repo.lookup_by_name(raw_host)
        cluster_name = node_info.cluster.name

        if self._bastion_type:
            bastion_type = self._bastion_type
        else:
            node_type = node_info.node_type
            bastion_type = self._node_type_map.get(node_type)
            if bastion_type is None:
                known = ", ".join(f"{k!r}→{v!r}" for k, v in self._node_type_map.items())
                raise CommandExecutionException(
                    f"node_type '{node_type}' has no bastion mapping. "
                    f"Known mappings: {{{known}}}. "
                    "Update BASTION_NODE_TYPE_MAP to include this node_type.",
                    detail={"node_type": node_type, "node_type_map": self._node_type_map},
                )

        mappings = await self._mapping_repo.list_mappings(bastion_type)

        for mapping in mappings:
            for pattern in mapping.patterns:
                try:
                    matched = re.fullmatch(pattern, cluster_name)
                except re.error:
                    _logger.warning(
                        "Skipping invalid regex pattern %r in bastion mapping "
                        "(type=%s) — fix the mapping API data",
                        pattern, bastion_type,
                    )
                    continue
                if matched:
                    return ResolvedHost(
                        ip=mapping.bastion_ip,
                        source_input=raw_host,
                        metadata={
                            "node_name": raw_host,
                            "cluster_name": cluster_name,
                            "bastion_hostname": mapping.bastion,
                            "bastion_type": bastion_type,
                            "matched_pattern": pattern,
                        },
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={
                "node_name": raw_host,
                "cluster_name": cluster_name,
                "bastion_type": bastion_type,
            },
        )


def create_host_resolver(
    host_type: HostType,
    *,
    inventory: Optional[InventoryRepository] = None,
    cluster_node_lookup_repo: Optional[ClusterNodeLookupRepository] = None,
    mapping_repo: Optional[BastionMappingRepository] = None,
    node_type_map: Optional[Dict[str, str]] = None,
    bastion_type: Optional[str] = None,
) -> HostResolver:
    if host_type == HostType.IP:
        return IpHostResolver()
    if host_type == HostType.HOSTNAME:
        if inventory is None:
            raise ValueError("HOSTNAME resolver requires inventory")
        return HostnameHostResolver(inventory)
    if host_type == HostType.BASTION:
        if cluster_node_lookup_repo is None or mapping_repo is None or node_type_map is None:
            raise ValueError(
                "BASTION resolver requires cluster_node_lookup_repo, mapping_repo, node_type_map"
            )
        return ClusterBastionHostResolver(
            cluster_node_lookup_repo, mapping_repo, node_type_map, bastion_type
        )
    raise ValueError(f"Unsupported host_type: {host_type}")
