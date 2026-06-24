import pytest
from app.domain.command import (
    CommandExecutionRequest, CommandWhitelistConfig, PipelineStep,
    SSHConnectionConfig, ExecutionContext, HostType,
)
from app.repositories.host_resolver import ResolvedHost
from app.services.command_service import CommandService


def _ctx(cmd_config, run_id=None):
    req = CommandExecutionRequest(
        command_name="run_ansible_ping_all", host="localhost",
        host_type=HostType.IP, port=2224, username="root",
        ssh_config="control_node",
        arguments={"inventory": "taipei/multinode.ini"},
    )
    ctx = ExecutionContext(
        username="admin", request_id="r1", command_name="run_ansible_ping_all",
        raw_request=req, cmd_config=cmd_config,
        ssh_config=SSHConnectionConfig(auth_method="key", key_base64="x"),
        resolved_host=ResolvedHost(ip="1.2.3.4", source_input="localhost"),
    )
    ctx.run_id = run_id
    return ctx


def _svc():
    return CommandService(repo=None, inventory_repo=None)


def test_logged_command_resolves_run_id_placeholder():
    cfg = CommandWhitelistConfig(
        command_name="run_ansible_ping_all", logged=True,
        pipeline=[PipelineStep(command=[
            "/x/run-ansible.sh", "--inventory", "{inventory}",
            "--log-dir", "/var/log/ansible-runs", "--run-id", "{run_id}",
        ])],
        arguments=[],
    )
    ctx = _ctx(cfg, run_id="abc-123")
    pipeline = _svc()._build_pipeline(ctx)
    flat = pipeline[0]
    assert "{run_id}" not in flat
    assert "abc-123" in flat
    assert flat[flat.index("--run-id") + 1] == "abc-123"


def test_compute_log_path():
    path = _svc()._compute_log_path("abc-123")
    assert path == "/var/log/ansible-runs/abc-123.log"
