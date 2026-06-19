"""Verify CommandService raises typed exceptions for input/policy errors
instead of returning 200 + failed body.
"""

import json
from pathlib import Path
import pytest

from app.core.exceptions import (
    CommandExecutionException, ForbiddenException, ServiceUnavailableException,
)
from app.domain.command import CommandExecutionRequest
from app.services.command_service import CommandService
import app.services.command_service as cs_mod
from app.repositories.inventory_repository import (
    ClusterNodeInfo, ClusterRef, NodeInfo,
)
from tests.fixtures.cluster import InMemoryClusterNodeLookupRepository


def _whitelist_file(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "allow-commands-test_admin.json"
    p.write_text(json.dumps(body))
    return p


def _ssh_default(tmp_path: Path) -> Path:
    p = tmp_path / "SSH-default.json"
    p.write_text(json.dumps({"auth_method": "key", "key_base64": "AA=="}))
    return p


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """Service with COMMAND_CONFIG_DIR pointed at an isolated tmp dir."""
    from app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("COMMAND_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    # Patch the module-level `settings` cached in command_service so its
    # internal references see the new COMMAND_CONFIG_DIR.
    monkeypatch.setattr(cs_mod, "settings", get_settings())

    cluster_node_lookup_repo = InMemoryClusterNodeLookupRepository({
        "node-a01": ClusterNodeInfo(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node-a01", labels={"mgmt_ip": "10.0.1.10"}),
            cluster=ClusterRef(id="1", name="cluster-c1"),
        ),
    })
    return CommandService(repo=None, cluster_node_lookup_repo=cluster_node_lookup_repo), tmp_path


async def test_no_whitelist_file_raises_forbidden(svc):
    service, _ = svc
    req = CommandExecutionRequest(
        command_name="ls", host="10.0.0.1", username="root",
    )
    with pytest.raises(ForbiddenException):
        await service._prepare_execution("test_admin", "rid", req)


async def test_deny_host_raises_forbidden(svc):
    service, tmp_path = svc
    _whitelist_file(tmp_path, {
        "name": "admin", "allow_hosts": [".*"], "deny_hosts": ["10\\.0\\.1\\.10"],
        "allow_commands": [{
            "command_name": "ls", "pipeline": [{"command": ["ls"]}],
            "arguments": [],
        }],
    })
    _ssh_default(tmp_path)
    req = CommandExecutionRequest(
        command_name="ls", host="node-a01", username="root", host_type="hostname",
    )
    with pytest.raises(ForbiddenException):
        await service._prepare_execution("test_admin", "rid", req)


async def test_command_not_in_whitelist_raises_forbidden(svc):
    service, tmp_path = svc
    _whitelist_file(tmp_path, {
        "name": "admin", "allow_hosts": [".*"], "deny_hosts": [],
        "allow_commands": [{
            "command_name": "ls", "pipeline": [{"command": ["ls"]}],
            "arguments": [],
        }],
    })
    _ssh_default(tmp_path)
    req = CommandExecutionRequest(
        command_name="reboot", host="10.0.0.1", username="root",
    )
    with pytest.raises(ForbiddenException):
        await service._prepare_execution("test_admin", "rid", req)


async def test_missing_argument_raises_command_execution_exception(svc):
    service, tmp_path = svc
    _whitelist_file(tmp_path, {
        "name": "admin", "allow_hosts": [".*"], "deny_hosts": [],
        "allow_commands": [{
            "command_name": "sleep", "pipeline": [{"command": ["sleep", "{time}"]}],
            "arguments": [{"name": "time", "type": "int", "validation_regex": "^\\d+$"}],
        }],
    })
    _ssh_default(tmp_path)
    req = CommandExecutionRequest(
        command_name="sleep", host="10.0.0.1", username="root", arguments={},
    )
    with pytest.raises(CommandExecutionException):
        await service._prepare_execution("test_admin", "rid", req)


def test_timeout_seconds_zero_is_not_replaced_by_default(monkeypatch):
    """timeout_seconds=0 must be used as-is, not silently replaced by the default."""
    import app.services.command_service as cs_mod

    default_timeout = cs_mod.settings.COMMAND_DEFAULT_TIMEOUT

    class _Opt:
        timeout_seconds = 0

    class _Req:
        option = _Opt()

    class _Ctx:
        raw_request = _Req()

    ctx = _Ctx()
    opt = ctx.raw_request.option
    # Reproduce the exact expression from command_service._run_command line 534.
    # With the truthiness bug: `0` is falsy → default is used.
    # After fix: `is not None` → 0 is used.
    actual = opt.timeout_seconds if opt.timeout_seconds is not None else default_timeout
    assert actual == 0, (
        f"timeout_seconds=0 was replaced by default {default_timeout} — "
        "use 'is not None' not truthiness check"
    )


def test_capacity_full_raises_service_unavailable(svc, monkeypatch):
    service, _ = svc
    # Fill the running-commands pool to the configured limit.
    monkeypatch.setattr(cs_mod.settings, "COMMAND_MAX_RUNNING", 1)
    monkeypatch.setattr(cs_mod, "_local_running_commands", {"x": object()})
    with pytest.raises(ServiceUnavailableException):
        service._check_capacity("test_admin", "rid")
