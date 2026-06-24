import pytest
from unittest.mock import AsyncMock, MagicMock
from app.domain.command import CommandState, CommandStatus
from app.services.command_service import CommandService
from app.core.exceptions import NotFoundException
from app.core.config import get_settings


def _state(**over):
    base = dict(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=2224, username="root",
        ssh_config="control_node", request_id="r1", exec_command="x",
        killable=True, run_log_path="/var/log/ansible-runs/c1.log",
    )
    base.update(over)
    return CommandState(**base)


def _svc_with_state(state):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=state)
    return CommandService(repo=repo, inventory_repo=None)


async def test_trace_no_log_path_returns_empty_with_status():
    svc = _svc_with_state(_state(run_log_path=None, status=CommandStatus.SUCCESS))
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.lines == []
    assert resp.status == "success"
    assert resp.total_size == 0
    assert resp.next_byte_offset == 0


async def test_trace_unknown_command_raises_notfound():
    repo = MagicMock()
    from app.core.exceptions import CommandExecutionException
    repo.get = AsyncMock(side_effect=CommandExecutionException("nope"))
    svc = CommandService(repo=repo, inventory_repo=None)
    with pytest.raises(NotFoundException):
        await svc.get_command_trace("missing")


async def test_trace_happy_path_renders_new_lines(monkeypatch):
    svc = _svc_with_state(_state())
    monkeypatch.setattr(
        svc, "_read_remote_log",
        AsyncMock(return_value=(18, "line one\nline two\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.total_size == 18
    assert resp.next_byte_offset == 18
    assert [l.num for l in resp.lines] == [1, 2]
    assert resp.next_line_num == 3


async def test_trace_hard_cap_stops_serving_lines(monkeypatch):
    get_settings.cache_clear()
    svc = _svc_with_state(_state())
    big = get_settings().COMMAND_LOG_HARD_CAP_BYTES + 1
    monkeypatch.setattr(
        svc, "_read_remote_log", AsyncMock(return_value=(big, "x\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.too_large is True
    assert resp.lines == []


async def test_trace_soft_cap_sets_warning_but_serves(monkeypatch):
    get_settings.cache_clear()
    svc = _svc_with_state(_state())
    mid = get_settings().COMMAND_LOG_SOFT_CAP_BYTES + 1
    monkeypatch.setattr(
        svc, "_read_remote_log", AsyncMock(return_value=(mid, "hello\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.size_warning is True
    assert resp.too_large is False
    assert len(resp.lines) == 1
