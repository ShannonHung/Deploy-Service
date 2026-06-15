"""Inventory API repository.

Defines the abstract InventoryRepository contract plus an HTTP-backed
implementation. The repository hides httpx from the service layer and
translates HTTP outcomes into the existing application exception
hierarchy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import httpx
from pydantic import BaseModel

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)


class InventoryBastion(BaseModel):
    hostname: str
    ip: str


class InventoryHostInfo(BaseModel):
    hostname: str
    ip: str
    bastion: InventoryBastion


class InventoryRepository(ABC):
    """Look up an Inventory record by hostname."""

    @abstractmethod
    async def lookup(self, hostname: str) -> InventoryHostInfo: ...


class HttpInventoryRepository(InventoryRepository):
    """HTTP-backed InventoryRepository.

    GET {base_url}/inventory/hosts/{hostname}
    Header: Authorization: Bearer {token}
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        # transport is injectable for testing (httpx.MockTransport).
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def lookup(self, hostname: str) -> InventoryHostInfo:
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"/inventory/hosts/{hostname}",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"Inventory lookup for '{hostname}' timed out after {self._timeout}s.",
                detail={"hostname": hostname},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"Inventory lookup for '{hostname}' failed: {exc}",
                detail={"hostname": hostname},
            ) from exc

        if resp.status_code == 404:
            raise NotFoundException(
                f"Host '{hostname}' not found in inventory.",
                detail={"hostname": hostname},
            )
        if resp.status_code >= 400:
            raise UpstreamUnavailableException(
                f"Inventory returned {resp.status_code} for '{hostname}'.",
                detail={"hostname": hostname, "status_code": resp.status_code},
            )

        return InventoryHostInfo.model_validate(resp.json())
