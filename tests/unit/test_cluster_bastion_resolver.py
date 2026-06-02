import pytest

from app.core.exceptions import NotFoundException
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.host_resolver import ClusterBastionHostResolver
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository,
    InMemoryVmRepository,
)


def _vm_repo(cluster_name: str) -> InMemoryVmRepository:
    return InMemoryVmRepository(
        {
            "node1": VmInfo(
                id=1,
                name="node1",
                k8s_cluster=VmK8sCluster(id=1, name=cluster_name),
            )
        }
    )


def _mapping_repo(mappings_by_type):
    return InMemoryBastionMappingRepository(mappings_by_type)


async def test_first_pattern_in_first_entry_wins():
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                runner="r1",
                bastion="b1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                pattern=["type1-kind"],
                runner="r2",
                bastion="b2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c1"), _mapping_repo(mappings), "type1"
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.0.0.1"
    assert resolved.source_input == "node1"
    assert resolved.metadata == {
        "node_name": "node1",
        "cluster_name": "type1-cluster-c1",
        "bastion_hostname": "b1",
        "bastion_type": "type1",
        "matched_pattern": "type1-cluster-(c1|c2|c3)",
    }


async def test_second_entry_priority_when_first_doesnt_match():
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)"],
                runner="r1",
                bastion="b1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                pattern=["type1-kind"],
                runner="r2",
                bastion="b2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-kind"), _mapping_repo(mappings), "type1"
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.0.0.2"
    assert resolved.metadata["matched_pattern"] == "type1-kind"


async def test_no_pattern_matches_raises_not_found():
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)"],
                runner="r1",
                bastion="b1",
                bastion_ip="10.0.0.1",
            ),
        ]
    }
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c99"), _mapping_repo(mappings), "type1"
    )
    with pytest.raises(NotFoundException) as exc_info:
        await resolver.resolve("node1")
    detail = exc_info.value.detail
    assert detail["node_name"] == "node1"
    assert detail["cluster_name"] == "type1-cluster-c99"
    assert detail["bastion_type"] == "type1"


async def test_vm_not_found_propagates():
    mappings = {"type1": [BastionMapping(pattern=[".*"], runner="r", bastion="b", bastion_ip="1.1.1.1")]}
    resolver = ClusterBastionHostResolver(
        InMemoryVmRepository({}),  # empty
        _mapping_repo(mappings),
        "type1",
    )
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing-node")


async def test_fullmatch_boundary_dotstar():
    """type1-cluster.* matches the whole string only when re.fullmatch is used."""
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster.*"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    # Matches: "type1-cluster-c1", "type1-cluster", "type1-clusterX"
    for cluster in ["type1-cluster-c1", "type1-cluster", "type1-clusterX"]:
        resolver = ClusterBastionHostResolver(
            _vm_repo(cluster), _mapping_repo(mappings), "type1"
        )
        resolved = await resolver.resolve("node1")
        assert resolved.ip == "10.0.0.1", f"should match {cluster!r}"


async def test_fullmatch_boundary_strict_alternation():
    """'type1-cluster-(c1|c2|c3)' under fullmatch matches '...c1' but not '...c99'."""
    mappings = {
        "type1": [
            BastionMapping(
                pattern=["type1-cluster-(c1|c2|c3)"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    # Matches c1
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c1"), _mapping_repo(mappings), "type1"
    )
    assert (await resolver.resolve("node1")).ip == "10.0.0.1"
    # Does NOT match c99 → no-match → NotFoundException
    resolver = ClusterBastionHostResolver(
        _vm_repo("type1-cluster-c99"), _mapping_repo(mappings), "type1"
    )
    with pytest.raises(NotFoundException):
        await resolver.resolve("node1")
