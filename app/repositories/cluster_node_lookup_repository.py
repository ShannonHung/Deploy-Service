"""Cluster-node lookup repository.

GET {base_url}/api/v1/k8s-clusters/node-cluster-lookup?node_name={node_name}

Contract:
  - 200 → ClusterNodeInfo with .cluster.name
  - 404  → NotFoundException
  - timeout → UpstreamTimeoutException
  - other 4xx / 5xx / net → UpstreamUnavailableException
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


class ClusterRef(BaseModel):
    id: str
    name: str


class ClusterNodeInfo(BaseModel):
    node_type: str
    node_name: str
    cluster: ClusterRef


class ClusterNodeLookupRepository(ABC):
    """Look up the cluster a node belongs to."""

    @abstractmethod
    async def lookup_by_name(self, node_name: str) -> ClusterNodeInfo: ...


class HttpClusterNodeLookupRepository(ClusterNodeLookupRepository):
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

    async def lookup_by_name(self, node_name: str) -> ClusterNodeInfo:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/k8s-clusters/node-cluster-lookup",
                    params={"node_name": node_name},
                    headers={"Authorization": f"Bearer {self._token}"},
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
