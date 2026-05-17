import pytest

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.host_resolver import (
    BastionHostResolver, HostnameHostResolver, IpHostResolver,
    ResolvedHost, create_host_resolver,
)
from app.repositories.inventory_repository import (
    InventoryBastion, InventoryHostInfo,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _inventory():
    return InMemoryInventoryRepository({
        "node-a01": InventoryHostInfo(
            hostname="node-a01", ip="10.0.1.10",
            bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
        ),
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


async def test_bastion_resolver_returns_bastion_ip():
    resolver = BastionHostResolver(_inventory())
    resolved = await resolver.resolve("node-a01")
    assert resolved.ip == "10.0.0.5"
    assert resolved.source_input == "node-a01"
    assert resolved.metadata == {
        "hostname": "node-a01",
        "bastion_hostname": "bastion-a",
    }


async def test_hostname_resolver_propagates_not_found():
    resolver = HostnameHostResolver(_inventory())
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing")


def test_factory_returns_correct_resolver_class():
    inv = _inventory()
    assert isinstance(create_host_resolver(HostType.IP, inv), IpHostResolver)
    assert isinstance(create_host_resolver(HostType.HOSTNAME, inv), HostnameHostResolver)
    assert isinstance(create_host_resolver(HostType.BASTION, inv), BastionHostResolver)
