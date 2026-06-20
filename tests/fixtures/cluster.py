"""In-memory InventoryRepository for tests."""

from typing import Dict, List

from app.core.exceptions import NotFoundException, UpstreamUnavailableException
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    InventoryRepository,
)


class InMemoryInventoryRepository(InventoryRepository):
    def __init__(
        self,
        nodes: Dict[str, ClusterNodeInfo] | None = None,
        mappings: Dict[str, List[BastionMapping]] | None = None,
    ) -> None:
        self._nodes = nodes or {}
        self._mappings = mappings or {}

    async def lookup_by_name(self, node_name: str) -> ClusterNodeInfo:
        if node_name not in self._nodes:
            raise NotFoundException(
                f"Node '{node_name}' not found.",
                detail={"node_name": node_name},
            )
        return self._nodes[node_name]

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        mappings = self._mappings.get(type_name, [])
        if not mappings:
            raise NotFoundException(
                f"No bastion mappings found for type '{type_name}'.",
                detail={"type": type_name},
            )
        return mappings


# Backward-compatible aliases — kept so that tests migrated incrementally
# can still import these names. New tests should use InMemoryInventoryRepository directly.

class InMemoryClusterNodeLookupRepository(InMemoryInventoryRepository):
    """Alias for InMemoryInventoryRepository (node-lookup-only constructor)."""

    def __init__(self, nodes: Dict[str, ClusterNodeInfo] | None = None) -> None:
        super().__init__(nodes=nodes, mappings={})


class InMemoryBastionMappingRepository(InMemoryInventoryRepository):
    """Alias for InMemoryInventoryRepository (mapping-only constructor)."""

    def __init__(self, mappings: Dict[str, List[BastionMapping]] | None = None) -> None:
        super().__init__(nodes={}, mappings=mappings)
