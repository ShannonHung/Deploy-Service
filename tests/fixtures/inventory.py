"""In-memory InventoryRepository for tests."""

from typing import Dict

from app.core.exceptions import NotFoundException
from app.repositories.inventory_repository import (
    InventoryHostInfo, InventoryRepository,
)


class InMemoryInventoryRepository(InventoryRepository):
    def __init__(self, records: Dict[str, InventoryHostInfo]):
        self._records = records

    async def lookup(self, hostname: str) -> InventoryHostInfo:
        info = self._records.get(hostname)
        if info is None:
            raise NotFoundException(
                f"Host '{hostname}' not found in inventory.",
                detail={"hostname": hostname},
            )
        return info
