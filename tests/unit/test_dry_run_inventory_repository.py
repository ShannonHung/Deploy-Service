"""
tests/unit/test_dry_run_inventory_repository.py

T3: DryRunInventoryRepository must satisfy the InventoryRepository contract,
and its canned data must be shaped so the *consumers* of that data keep
working — the IP label the host resolvers read, and a node_type the configured
BASTION_NODE_TYPE_MAP knows.

See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import inspect

import pytest

from app.repositories.dry_run_inventory_repository import DryRunInventoryRepository
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    InventoryRepository,
)


@pytest.fixture
def repo() -> DryRunInventoryRepository:
    return DryRunInventoryRepository()


# ── contract conformance ──────────────────────────────────────────────────────


def test_implements_every_abstract_method():
    abstract = {
        name
        for name, value in inspect.getmembers(InventoryRepository)
        if getattr(value, "__isabstractmethod__", False)
    }
    assert abstract, "expected InventoryRepository to declare abstract methods"
    assert not abstract - set(dir(DryRunInventoryRepository))
    assert isinstance(DryRunInventoryRepository(), InventoryRepository)


async def test_lookup_returns_cluster_node_info(repo):
    result = await repo.lookup_by_name("node1")
    assert isinstance(result, ClusterNodeInfo)


async def test_lookup_echoes_requested_node_name(repo):
    """Echoing keeps the response self-consistent, so a caller sees the node it
    actually asked for."""
    assert (await repo.lookup_by_name("node-42")).node.name == "node-42"


async def test_list_mappings_returns_mappings(repo):
    result = await repo.list_mappings("type1")
    assert result and all(isinstance(m, BastionMapping) for m in result)


async def test_list_mappings_is_never_empty(repo):
    """The real contract raises NotFoundException on an empty result, which
    would make every dry-run bastion resolution fail."""
    assert await repo.list_mappings("anything") != []


# ── shape the consumers depend on ─────────────────────────────────────────────


async def test_node_carries_the_configured_ip_label(repo):
    """NodeNameHostResolver extracts the SSH target from this label; without it
    host resolution raises CommandExecutionException."""
    result = await repo.lookup_by_name("node1")
    assert "mgmt_ip" in result.node.labels


async def test_ip_label_is_configurable():
    """INVENTORY_IP_LABEL is a setting, so the stub must honour it rather than
    hardcoding the default."""
    repo = DryRunInventoryRepository(ip_label="custom_ip")
    assert "custom_ip" in (await repo.lookup_by_name("n")).node.labels


async def test_mapping_pattern_matches_the_canned_cluster_name(repo):
    """InventoryService regex-matches patterns against the cluster name. If no
    pattern matched, resolution would raise NotFoundException."""
    import re

    node = await repo.lookup_by_name("node1")
    mappings = await repo.list_mappings("type1")
    assert any(
        re.fullmatch(p, node.cluster.name)
        for m in mappings
        for p in m.patterns
    )


# ── synthetic values ──────────────────────────────────────────────────────────


async def test_addresses_are_non_routable_documentation_ips(repo):
    """RFC 5737 TEST-NET-1: a leaked dry-run address fails to connect rather
    than reaching a live host."""
    node = await repo.lookup_by_name("node1")
    mappings = await repo.list_mappings("type1")
    assert node.node.labels["mgmt_ip"].startswith("192.0.2.")
    assert mappings[0].bastion_ip.startswith("192.0.2.")
