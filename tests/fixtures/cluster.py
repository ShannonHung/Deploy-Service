"""In-memory VmRepository + BastionMappingRepository for tests."""

from typing import Dict, List

from app.core.exceptions import NotFoundException
from app.repositories.bastion_mapping_repository import (
    BastionMapping,
    BastionMappingRepository,
)
from app.repositories.vm_repository import VmInfo, VmRepository


class InMemoryVmRepository(VmRepository):
    def __init__(self, records: Dict[str, VmInfo]):
        self._records = records

    async def lookup_by_name(self, node_name: str) -> VmInfo:
        info = self._records.get(node_name)
        if info is None:
            raise NotFoundException(
                f"VM '{node_name}' not found.",
                detail={"node_name": node_name},
            )
        return info


class InMemoryBastionMappingRepository(BastionMappingRepository):
    def __init__(self, mappings_by_type: Dict[str, List[BastionMapping]]):
        self._mappings_by_type = mappings_by_type

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        mappings = self._mappings_by_type.get(type_name)
        if not mappings:
            raise NotFoundException(
                f"No bastion mappings found for type '{type_name}'.",
                detail={"type": type_name},
            )
        return mappings
