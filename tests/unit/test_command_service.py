import pytest
from app.services.command_service import CommandService, CommandExecutionException

def test_anti_injection_pass():
    svc = CommandService(None, None)  # repo and inventory not used for this method
    svc._validate_anti_injection("safe_string_123")

def test_anti_injection_fail():
    svc = CommandService(None, None)
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("ls; rm -rf /")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("$(whoami)")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("a & b")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("a | b")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("`whoami`")


# ── bastion_type wiring ────────────────────────────────────────────────────

import app.services.command_service as svc_module
from unittest.mock import AsyncMock, MagicMock

from app.domain.command import (
    CommandExecutionRequest, CommandOption, HostType,
)
from app.repositories.inventory_repository import BastionMapping
from app.repositories.inventory_repository import ClusterNodeInfo, ClusterRef
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository, InMemoryClusterNodeLookupRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _service_for_bastion(cluster_node_lookup_repo, mapping_repo):
    """Build a CommandService with minimum deps for _prepare_execution to run."""
    state_repo = MagicMock()
    state_repo.save = AsyncMock()
    return CommandService(
        repo=state_repo,
        inventory=InMemoryInventoryRepository({}),
        cluster_node_lookup_repo=cluster_node_lookup_repo,
        mapping_repo=mapping_repo,
    )


def _node_lookup(cluster: str, node_type: str = "baremetal") -> InMemoryClusterNodeLookupRepository:
    return InMemoryClusterNodeLookupRepository({
        "n1": ClusterNodeInfo(
            node_type=node_type,
            node_name="n1",
            cluster=ClusterRef(id="1", name=cluster),
        )
    })


def _mapping(type_name: str, ip: str) -> InMemoryBastionMappingRepository:
    return InMemoryBastionMappingRepository({
        type_name: [BastionMapping(
            patterns=[".*"], runner="r", bastion="b", bastion_ip=ip,
        )]
    })


async def test_bastion_type_explicit_in_option_overrides_node_type_map(monkeypatch):
    """When option.bastion_type='type2' is set, it overrides node_type_map lookup."""
    monkeypatch.setattr(svc_module.settings, "BASTION_NODE_TYPE_MAP", {"baremetal": "type1"})
    node_lookup = _node_lookup("any-cluster", node_type="baremetal")
    # Only 'type2' has a mapping; if the resolver used node_type_map it would pick type1 and 404.
    mapping = _mapping("type2", "10.10.10.10")
    svc = _service_for_bastion(node_lookup, mapping)

    req = CommandExecutionRequest(
        command_name="list_file", host="n1", host_type=HostType.BASTION,
        port=22, username="root", ssh_config="default",
        option=CommandOption(timeout_seconds=30, bastion_type="type2"),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.10.10.10"
    assert ctx.resolved_host.metadata["bastion_type"] == "type2"


async def test_bastion_type_derived_from_node_type_map_when_no_option(monkeypatch):
    """When option.bastion_type is absent, node_type is looked up in BASTION_NODE_TYPE_MAP."""
    monkeypatch.setattr(svc_module.settings, "BASTION_NODE_TYPE_MAP", {"baremetal": "type1"})
    node_lookup = _node_lookup("any-cluster", node_type="baremetal")
    mapping = _mapping("type1", "10.20.30.40")
    svc = _service_for_bastion(node_lookup, mapping)

    req = CommandExecutionRequest(
        command_name="list_file", host="n1", host_type=HostType.BASTION,
        port=22, username="root", ssh_config="default",
        option=CommandOption(timeout_seconds=30),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.20.30.40"
    assert ctx.resolved_host.metadata["bastion_type"] == "type1"


async def test_unknown_node_type_not_in_map_raises_with_clear_message(monkeypatch):
    """When node_type has no entry in BASTION_NODE_TYPE_MAP, raise CommandExecutionException
    with the unknown node_type and the current map contents in the message."""
    monkeypatch.setattr(svc_module.settings, "BASTION_NODE_TYPE_MAP", {"baremetal": "type1"})
    node_lookup = _node_lookup("any-cluster", node_type="unknown-hw")
    mapping = _mapping("type1", "10.0.0.1")
    svc = _service_for_bastion(node_lookup, mapping)

    req = CommandExecutionRequest(
        command_name="list_file", host="n1", host_type=HostType.BASTION,
        port=22, username="root", ssh_config="default",
        arguments={"key_word": "ssh"},
    )

    with pytest.raises(CommandExecutionException) as exc_info:
        await svc._prepare_execution("test_admin", "rid", req)

    msg = str(exc_info.value)
    assert "unknown-hw" in msg
    assert "baremetal" in msg
