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

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.bastion_mapping_repository import BastionMappingRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.vm_repository import VmRepository

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
    """Resolve node_name → cluster_name → bastion_ip via two API calls."""

    def __init__(
        self,
        vm_repo: VmRepository,
        mapping_repo: BastionMappingRepository,
        bastion_type: str,
    ) -> None:
        self._vm_repo = vm_repo
        self._mapping_repo = mapping_repo
        self._bastion_type = bastion_type

    async def resolve(self, raw_host: str) -> ResolvedHost:
        vm = await self._vm_repo.lookup_by_name(raw_host)
        cluster_name = vm.k8s_cluster.name

        mappings = await self._mapping_repo.list_mappings(self._bastion_type)

        for mapping in mappings:
            for pattern in mapping.pattern:
                try:
                    matched = re.fullmatch(pattern, cluster_name)
                except re.error:
                    _logger.warning(
                        "Skipping invalid regex pattern %r in bastion mapping "
                        "(type=%s) — fix the mapping API data",
                        pattern, self._bastion_type,
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
                            "bastion_type": self._bastion_type,
                            "matched_pattern": pattern,
                        },
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{self._bastion_type}'.",
            detail={
                "node_name": raw_host,
                "cluster_name": cluster_name,
                "bastion_type": self._bastion_type,
            },
        )


def create_host_resolver(
    host_type: HostType,
    *,
    inventory: Optional[InventoryRepository] = None,
    vm_repo: Optional[VmRepository] = None,
    mapping_repo: Optional[BastionMappingRepository] = None,
    bastion_type: Optional[str] = None,
) -> HostResolver:
    if host_type == HostType.IP:
        return IpHostResolver()
    if host_type == HostType.HOSTNAME:
        if inventory is None:
            raise ValueError("HOSTNAME resolver requires inventory")
        return HostnameHostResolver(inventory)
    if host_type == HostType.BASTION:
        if vm_repo is None or mapping_repo is None or bastion_type is None:
            raise ValueError(
                "BASTION resolver requires vm_repo, mapping_repo, bastion_type"
            )
        return ClusterBastionHostResolver(vm_repo, mapping_repo, bastion_type)
    raise ValueError(f"Unsupported host_type: {host_type}")
