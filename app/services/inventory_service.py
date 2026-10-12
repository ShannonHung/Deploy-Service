"""Inventory resolution service."""

from __future__ import annotations

import logging
from typing import Dict, Optional

from app.core.exceptions import NotFoundException
from app.repositories.bastion_matching import bastion_type_for_node, match_bastion
from app.repositories.host_resolver import cluster_type_from_name
from app.repositories.inventory_repository import (
    ClusterBastionResolution,
    InventoryRepository,
    NodeBastionResolution,
)

_logger = logging.getLogger(__name__)


class InventoryService:
    def __init__(
        self,
        repo: InventoryRepository,
        node_type_map: Dict[str, str],
        slash_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self._repo = repo
        self._node_type_map = node_type_map
        self._slash_map = slash_map or {}

    async def resolve_node_bastion(
        self,
        node_name: str,
        bastion_type_override: Optional[str] = None,
    ) -> NodeBastionResolution:
        node_info = await self._repo.lookup_by_name(node_name)
        cluster_name = node_info.cluster.name

        if bastion_type_override is not None:
            bastion_type = bastion_type_override
            bastion_type_source = "query_param"
        else:
            bastion_type = bastion_type_for_node(
                node_info.node_type, self._node_type_map
            )
            bastion_type_source = "config"

        match = await match_bastion(self._repo, bastion_type, cluster_name)
        if match is not None:
            return NodeBastionResolution(
                node_type=node_info.node_type,
                node=node_info.node,
                cluster=node_info.cluster,
                bastion_type=bastion_type,
                bastion_type_source=bastion_type_source,
                matched_mapping=match.mapping,
                matched_pattern=match.pattern,
            )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={
                "node_name": node_name,
                "cluster_name": cluster_name,
                "bastion_type": bastion_type,
            },
        )

    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        bastion_type, has_slash = cluster_type_from_name(cluster_name, self._slash_map)
        match = await match_bastion(self._repo, bastion_type, cluster_name)
        if match is not None:
            return ClusterBastionResolution(
                cluster_name=cluster_name,
                has_slash=has_slash,
                bastion_type=bastion_type,
                matched_mapping=match.mapping,
                matched_pattern=match.pattern,
            )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={"cluster_name": cluster_name, "bastion_type": bastion_type},
        )
