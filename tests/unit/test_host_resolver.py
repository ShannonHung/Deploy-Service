import pytest

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.host_resolver import (
    ClusterBastionHostResolver,
    HostnameHostResolver,
    IpHostResolver,
    ResolvedHost,
    create_host_resolver,
)
from app.repositories.inventory_repository import (
    InventoryBastion,
    InventoryHostInfo,
)
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository,
    InMemoryVmRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _inventory():
    return InMemoryInventoryRepository({
        "node-a01": InventoryHostInfo(
            hostname="node-a01", ip="10.0.1.10",
            bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
        ),
    })


def _vm_repo():
    return InMemoryVmRepository({
        "node1": VmInfo(
            id=1, name="node1",
            k8s_cluster=VmK8sCluster(id=1, name="type1-cluster-c1"),
        ),
    })


def _mapping_repo():
    return InMemoryBastionMappingRepository({
        "type1": [
            BastionMapping(
                pattern=["type1-cluster.*"],
                runner="r", bastion="b", bastion_ip="10.0.0.1",
            )
        ]
    })


async def test_ip_resolver_returns_input_unchanged():
    resolver = IpHostResolver()
    resolved = await resolver.resolve("10.0.0.1")
    assert resolved == ResolvedHost(ip="10.0.0.1", source_input="10.0.0.1", metadata={})


async def test_hostname_resolver_returns_host_ip():
    resolver = HostnameHostResolver(_inventory())
    resolved = await resolver.resolve("node-a01")
    assert resolved.ip == "10.0.1.10"
    assert resolved.source_input == "node-a01"
    assert resolved.metadata == {"hostname": "node-a01"}


async def test_hostname_resolver_propagates_not_found():
    resolver = HostnameHostResolver(_inventory())
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing")


def test_factory_returns_correct_resolver_class():
    assert isinstance(
        create_host_resolver(HostType.IP), IpHostResolver
    )
    assert isinstance(
        create_host_resolver(HostType.HOSTNAME, inventory=_inventory()),
        HostnameHostResolver,
    )
    assert isinstance(
        create_host_resolver(
            HostType.BASTION,
            vm_repo=_vm_repo(),
            mapping_repo=_mapping_repo(),
            bastion_type="type1",
        ),
        ClusterBastionHostResolver,
    )


def test_factory_bastion_missing_deps_raises():
    with pytest.raises(ValueError):
        create_host_resolver(HostType.BASTION)
