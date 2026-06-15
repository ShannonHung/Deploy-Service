"""Bastion-Cluster mapping API repository.

GET {base_url}/api/v1/bastion-cluster-mappings?type={type_name}

Contract:
  - 200 + non-empty results → list[BastionMapping] (priority preserved)
  - 200 + empty results     → NotFoundException (unknown type)
  - 200 + malformed body    → UpstreamUnavailableException
  - timeout                 → UpstreamTimeoutException
  - other 4xx / 5xx / net   → UpstreamUnavailableException
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

import httpx
from pydantic import BaseModel

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)


class BastionMapping(BaseModel):
    pattern: List[str]
    runner: str
    bastion: str
    bastion_ip: str


class BastionMappingRepository(ABC):
    """List bastion-cluster mappings for a given type."""

    @abstractmethod
    async def list_mappings(self, type_name: str) -> List[BastionMapping]: ...


class HttpBastionMappingRepository(BastionMappingRepository):
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
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/bastion-cluster-mappings",
                    params={"type": type_name},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"Bastion mapping lookup for type '{type_name}' "
                f"timed out after {self._timeout}s.",
                detail={"type": type_name},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"Bastion mapping lookup for type '{type_name}' failed: {exc}",
                detail={"type": type_name},
            ) from exc

        if resp.status_code >= 400:
            raise UpstreamUnavailableException(
                f"Bastion mapping API returned {resp.status_code} for type '{type_name}'.",
                detail={"type": type_name, "status_code": resp.status_code},
            )

        try:
            payload = resp.json()
        except Exception:
            raise UpstreamUnavailableException(
                f"Bastion mapping API returned non-JSON response for type '{type_name}'.",
                detail={"type": type_name},
            )
        if not isinstance(payload, dict) or "results" not in payload:
            raise UpstreamUnavailableException(
                f"Bastion mapping API returned unexpected payload shape for type '{type_name}'.",
                detail={"type": type_name},
            )
        results = payload["results"]
        if not results:
            raise NotFoundException(
                f"No bastion mappings found for type '{type_name}'.",
                detail={"type": type_name},
            )
        return [BastionMapping.model_validate(item) for item in results]
