"""Node → Bastion matching shared by every caller that needs to find a Bastion.

The inventory endpoints (InventoryService) and the host resolvers all answer the
same question — "which Bastion fronts this cluster?" — so the rules live here
once: a Bastion type picks the mapping list, the first pattern that fully
matches the cluster name wins, and "no match" is the same NotFound for every
caller.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

from pydantic import BaseModel

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.repositories.inventory_repository import BastionMapping, InventoryRepository

_logger = logging.getLogger(__name__)


class BastionMatch(BaseModel):
    mapping: BastionMapping
    pattern: str


def bastion_type_for_node(node_type: str, node_type_map: Dict[str, str]) -> str:
    """Map a node_type to its Bastion type; unmapped is an operator misconfig."""
    bastion_type = node_type_map.get(node_type)
    if bastion_type is None:
        known = ", ".join(f"{k!r}→{v!r}" for k, v in node_type_map.items())
        raise CommandExecutionException(
            f"node_type '{node_type}' has no bastion mapping. "
            f"Known mappings: {{{known}}}. "
            "Update BASTION_NODE_TYPE_MAP to include this node_type.",
            detail={"node_type": node_type, "node_type_map": node_type_map},
        )
    return bastion_type


def pick_bastion_type(
    override: Optional[str], node_type: str, node_type_map: Dict[str, str]
) -> str:
    """An explicit Bastion type wins; otherwise derive it from the node_type."""
    if override is not None:
        return override
    return bastion_type_for_node(node_type, node_type_map)


async def match_bastion(
    repo: InventoryRepository, bastion_type: str, cluster_name: str
) -> Optional[BastionMatch]:
    """Return the first mapping whose pattern fully matches, else None.

    Propagates the repository's own errors (no mappings for the type, upstream
    failures). Invalid regex patterns are inventory data errors: skipped with a
    warning rather than failing every request of that type.
    """
    mappings = await repo.list_mappings(bastion_type)
    for mapping in mappings:
        for pattern in mapping.patterns:
            try:
                matched = re.fullmatch(pattern, cluster_name)
            except re.error:
                _logger.warning(
                    "Skipping invalid regex pattern %r in bastion mapping "
                    "(type=%s) — fix the mapping API data",
                    pattern,
                    bastion_type,
                )
                continue
            if matched:
                return BastionMatch(mapping=mapping, pattern=pattern)
    return None


async def require_bastion(
    repo: InventoryRepository,
    bastion_type: str,
    cluster_name: str,
    *,
    node_name: Optional[str] = None,
) -> BastionMatch:
    """Like match_bastion, but "no Bastion fronts this cluster" is a 404."""
    match = await match_bastion(repo, bastion_type, cluster_name)
    if match is not None:
        return match
    detail = {"cluster_name": cluster_name, "bastion_type": bastion_type}
    if node_name is not None:
        detail = {"node_name": node_name, **detail}
    raise NotFoundException(
        f"No bastion mapping matched cluster '{cluster_name}' "
        f"for type '{bastion_type}'.",
        detail=detail,
    )
