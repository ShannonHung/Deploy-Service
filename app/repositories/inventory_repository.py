"""Inventory API repository.

Single HTTP client for all inventory service endpoints:
  - GET /inventory/hosts/{hostname}
  - GET /api/v1/k8s-clusters/node-cluster-lookup?node_name={name}
  - GET /api/v1/bastion-cluster-mappings?name={name}

Contract per endpoint:
  - lookup_host:      200 → InventoryHostInfo; 404 → NotFoundException
  - lookup_by_name:   200 → ClusterNodeInfo;   404 → NotFoundException
  - list_mappings:    200 + exactly 1 result → list[BastionMapping] from result["data"]
                      200 + 0 results → NotFoundException
                      200 + >1 results → UpstreamUnavailableException
  All endpoints:      timeout → UpstreamTimeoutException
                      other 4xx/5xx/net → UpstreamUnavailableException
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


# ── Models ────────────────────────────────────────────────────────────────────

class InventoryBastion(BaseModel):
    hostname: str
    ip: str


class InventoryHostInfo(BaseModel):
    hostname: str
    ip: str
    bastion: InventoryBastion


class ClusterRef(BaseModel):
    id: str
    name: str


class ClusterNodeInfo(BaseModel):
    node_type: str
    node_name: str
    cluster: ClusterRef


class BastionMapping(BaseModel):
    patterns: List[str]
    runner: str
    bastion: str
    bastion_ip: str


# ── Abstract interfaces ───────────────────────────────────────────────────────

class InventoryRepository(ABC):
    """Look up a host record by hostname."""

    @abstractmethod
    async def lookup(self, hostname: str) -> InventoryHostInfo: ...


class ClusterNodeLookupRepository(ABC):
    """Look up the cluster a node belongs to."""

    @abstractmethod
    async def lookup_by_name(self, node_name: str) -> ClusterNodeInfo: ...


class BastionMappingRepository(ABC):
    """List bastion-cluster mappings for a given type."""

    @abstractmethod
    async def list_mappings(self, type_name: str) -> List[BastionMapping]: ...


# ── HTTP implementation ───────────────────────────────────────────────────────

class HttpInventoryRepository(
    InventoryRepository, ClusterNodeLookupRepository, BastionMappingRepository
):
    """Single httpx client implementing all three inventory API interfaces."""

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

    # ── InventoryRepository ───────────────────────────────────────────────────

    async def lookup(self, hostname: str) -> InventoryHostInfo:
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"/inventory/hosts/{hostname}",
                    headers={"Authorization": f"Token {self._token}"},
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

    # ── ClusterNodeLookupRepository ───────────────────────────────────────────

    async def lookup_by_name(self, node_name: str) -> ClusterNodeInfo:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/k8s-clusters/node-cluster-lookup",
                    params={"node_name": node_name},
                    headers={"Authorization": f"Token {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"Cluster node lookup for '{node_name}' timed out after {self._timeout}s.",
                detail={"node_name": node_name},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"Cluster node lookup for '{node_name}' failed: {exc}",
                detail={"node_name": node_name},
            ) from exc

        if resp.status_code == 404:
            raise NotFoundException(
                f"Node '{node_name}' not found in cluster lookup.",
                detail={"node_name": node_name},
            )
        if resp.status_code >= 400:
            raise UpstreamUnavailableException(
                f"Cluster node lookup API returned {resp.status_code} for '{node_name}'.",
                detail={"node_name": node_name, "status_code": resp.status_code},
            )

        try:
            payload = resp.json()
        except Exception:
            raise UpstreamUnavailableException(
                f"Cluster node lookup API returned non-JSON response for '{node_name}'.",
                detail={"node_name": node_name},
            )

        try:
            return ClusterNodeInfo.model_validate(payload)
        except Exception:
            raise UpstreamUnavailableException(
                f"Cluster node lookup API returned unexpected payload shape for '{node_name}'.",
                detail={"node_name": node_name},
            )

    # ── BastionMappingRepository ──────────────────────────────────────────────

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/bastion-cluster-mappings",
                    params={"name": type_name},
                    headers={"Authorization": f"Token {self._token}"},
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
        if len(results) != 1:
            raise UpstreamUnavailableException(
                f"Bastion mapping API returned {len(results)} results for type '{type_name}'; expected exactly 1.",
                detail={"type": type_name, "count": len(results)},
            )
        data = results[0].get("data")
        if not isinstance(data, list):
            raise UpstreamUnavailableException(
                f"Bastion mapping API result missing 'data' list for type '{type_name}'.",
                detail={"type": type_name},
            )
        return [BastionMapping.model_validate(item) for item in data]
