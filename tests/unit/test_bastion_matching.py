"""Shared node → Bastion matching: the one place a cluster name is matched
against the bastion mappings of a Bastion type."""

import pytest

from app.core.exceptions import CommandExecutionException
from app.repositories.bastion_matching import bastion_type_for_node, match_bastion
from app.repositories.inventory_repository import BastionMapping
from tests.fixtures.cluster import InMemoryInventoryRepository

_B1 = BastionMapping(
    patterns=["[bad", "prod-.*"], runner="r1", bastion="b1", bastion_ip="10.1.1.1"
)
_B2 = BastionMapping(patterns=["dev-.*"], runner="r2", bastion="b2", bastion_ip="10.1.1.2")


def _repo():
    return InMemoryInventoryRepository(mappings={"type1": [_B1, _B2]})


async def test_returns_first_mapping_whose_pattern_fully_matches():
    match = await match_bastion(_repo(), "type1", "dev-c1")
    assert match.mapping.bastion == "b2"
    assert match.pattern == "dev-.*"


async def test_invalid_regex_is_skipped_not_fatal():
    match = await match_bastion(_repo(), "type1", "prod-c1")
    assert match.mapping.bastion == "b1"
    assert match.pattern == "prod-.*"


async def test_no_matching_pattern_returns_none():
    assert await match_bastion(_repo(), "type1", "other") is None


async def test_partial_match_is_not_a_match():
    assert await match_bastion(_repo(), "type1", "xdev-c1") is None


def test_bastion_type_from_node_type_map():
    assert bastion_type_for_node("baremetal", {"baremetal": "type1"}) == "type1"


def test_unmapped_node_type_raises():
    with pytest.raises(CommandExecutionException) as exc:
        bastion_type_for_node("vm", {"baremetal": "type1"})
    assert exc.value.detail["node_type"] == "vm"
