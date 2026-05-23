"""Host resolver strategy: chooses the SSH target IP based on host_type.

Adding a new host type:
  1. Add a value to HostType in app/domain/command.py.
  2. Add a HostResolver subclass here.
  3. Add a branch to create_host_resolver().
CommandService does not need to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

from pydantic import BaseModel, Field

from app.domain.command import HostType
from app.repositories.inventory_repository import InventoryRepository


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


class BastionHostResolver(HostResolver):
    def __init__(self, inventory: InventoryRepository) -> None:
        self._inventory = inventory

    async def resolve(self, raw_host: str) -> ResolvedHost:
        info = await self._inventory.lookup(raw_host)
        return ResolvedHost(
            ip=info.bastion.ip,
            source_input=raw_host,
            metadata={
                "hostname": info.hostname,
                "bastion_hostname": info.bastion.hostname,
            },
        )


def create_host_resolver(
    host_type: HostType, inventory: InventoryRepository,
) -> HostResolver:
    if host_type == HostType.IP:
        return IpHostResolver()
    if host_type == HostType.HOSTNAME:
        return HostnameHostResolver(inventory)
    if host_type == HostType.BASTION:
        return BastionHostResolver(inventory)
    raise ValueError(f"Unsupported host_type: {host_type}")
