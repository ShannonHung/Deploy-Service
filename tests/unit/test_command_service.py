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

from unittest.mock import AsyncMock, MagicMock

from app.core.config import get_settings
from app.domain.command import (
    CommandExecutionRequest, CommandOption, HostType,
)
from app.repositories.bastion_mapping_repository import BastionMapping
from app.repositories.vm_repository import VmInfo, VmK8sCluster
from tests.fixtures.cluster import (
    InMemoryBastionMappingRepository, InMemoryVmRepository,
)
from tests.fixtures.inventory import InMemoryInventoryRepository


def _service_for_bastion(vm_repo, mapping_repo):
    """Build a CommandService with minimum deps for _prepare_execution to run."""
    state_repo = MagicMock()
    state_repo.save = AsyncMock()
    return CommandService(
        repo=state_repo,
        inventory=InMemoryInventoryRepository({}),
        vm_repo=vm_repo,
        mapping_repo=mapping_repo,
    )


def _vm(cluster: str) -> InMemoryVmRepository:
    return InMemoryVmRepository({
        "n1": VmInfo(id=1, name="n1", k8s_cluster=VmK8sCluster(id=1, name=cluster))
    })


def _mapping(type_name: str, ip: str) -> InMemoryBastionMappingRepository:
    return InMemoryBastionMappingRepository({
        type_name: [BastionMapping(
            pattern=[".*"], runner="r", bastion="b", bastion_ip=ip,
        )]
    })


async def test_bastion_type_explicit_in_option_is_used():
    """When option.bastion_type='type2' is set, mapping_repo is called with 'type2'."""
    vm = _vm("type2-cluster-x")
    # Only 'type2' has a mapping; if the resolver asked for any other type it would 404.
    mapping = _mapping("type2", "10.10.10.10")
    svc = _service_for_bastion(vm, mapping)

    req = CommandExecutionRequest(
        command_name="list_file", host="n1", host_type=HostType.BASTION,
        port=22, username="root", ssh_config="default",
        option=CommandOption(timeout_seconds=30, bastion_type="type2"),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.10.10.10"
    assert ctx.resolved_host.metadata["bastion_type"] == "type2"


async def test_bastion_type_defaults_to_settings_when_option_none():
    """When option.bastion_type is None, mapping_repo is called with BASTION_DEFAULT_TYPE."""
    default_type = get_settings().BASTION_DEFAULT_TYPE
    vm = _vm("anything")
    mapping = _mapping(default_type, "10.20.30.40")
    svc = _service_for_bastion(vm, mapping)

    req = CommandExecutionRequest(
        command_name="list_file", host="n1", host_type=HostType.BASTION,
        port=22, username="root", ssh_config="default",
        option=CommandOption(timeout_seconds=30),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.20.30.40"
    assert ctx.resolved_host.metadata["bastion_type"] == default_type
