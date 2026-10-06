"""
app/repositories/dry_run_inventory_repository.py

Dry-run implementation of InventoryRepository — returns canned data and never
talks to the inventory API.

Used only when ``DRY_RUN_MODE=true`` (e2e pipeline testing). Substituted for
``HttpInventoryRepository`` at the single ``get_inventory_repository`` DI
factory, so everything that consumes it keeps running for real:
``InventoryService``'s node_type → bastion_type mapping and regex
pattern-matching, and the ``HostResolver`` implementations' IP-label extraction.

Only the data *source* is replaced; none of the resolution *logic* is bypassed.
This is also the prerequisite for the command path (T4): ``_prepare_execution``
resolves the target host through this repository, so its network dependency has
to go before an SSH command can run in dry-run.

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import logging
from typing import List

from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    ClusterRef,
    InventoryRepository,
    NodeInfo,
)

_logger = logging.getLogger(__name__)

# Addresses are in RFC 5737 TEST-NET-1 (192.0.2.0/24), which is reserved for
# documentation and is not routable. A dry-run IP that leaks into a real
# connection attempt fails immediately instead of reaching a live host.
_DRY_RUN_NODE_IP = "192.0.2.10"
_DRY_RUN_BASTION_IP = "192.0.2.20"

# Matches any cluster name, so the regex matching in InventoryService /
# ClusterBastionHostResolver still executes and still has to succeed — a
# mapping that failed to match would surface as a real NotFoundException.
_MATCH_ALL_PATTERN = ".*"


class DryRunInventoryRepository(InventoryRepository):
    """Canned InventoryRepository used when DRY_RUN_MODE is enabled.

    The returned shapes are deliberately *valid* rather than minimal: the node
    carries the configured IP label so ``NodeNameHostResolver`` can extract an
    address, and ``node_type`` is one the configured ``BASTION_NODE_TYPE_MAP``
    knows, so bastion resolution reaches a mapping instead of raising.
    """

    def __init__(
        self,
        node_type: str = "baremetal",
        cluster_name: str = "dry-run-cluster",
        ip_label: str = "mgmt_ip",
    ) -> None:
        self._node_type = node_type
        self._cluster_name = cluster_name
        self._ip_label = ip_label

    async def lookup_by_name(self, node_name: str) -> ClusterNodeInfo:
        """Return a canned node that echoes the requested name.

        Echoing *node_name* keeps the response self-consistent, so a caller
        inspecting the resolution sees the node it actually asked for.
        """
        _logger.info(
            "DRY-RUN | op=inventory.lookup_by_name | node=%s | no API call made",
            node_name,
        )
        return ClusterNodeInfo(
            node_type=self._node_type,
            node=NodeInfo(
                id="dry-run-node-id",
                name=node_name,
                # The IP label is what NodeNameHostResolver extracts; without it
                # host resolution would raise CommandExecutionException.
                labels={self._ip_label: _DRY_RUN_NODE_IP},
            ),
            cluster=ClusterRef(
                id="dry-run-cluster-id",
                name=self._cluster_name,
                context="dry-run-context",
            ),
        )

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        """Return a single match-all mapping for *type_name*.

        One mapping is returned rather than none: the real contract raises
        NotFoundException on an empty result, which would make every dry-run
        bastion resolution fail.
        """
        _logger.info(
            "DRY-RUN | op=inventory.list_mappings | type=%s | no API call made",
            type_name,
        )
        return [
            BastionMapping(
                patterns=[_MATCH_ALL_PATTERN],
                runner=f"dry-run-runner-{type_name}",
                bastion=f"dry-run-bastion-{type_name}",
                bastion_ip=_DRY_RUN_BASTION_IP,
            )
        ]
