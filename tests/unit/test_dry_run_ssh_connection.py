"""
tests/unit/test_dry_run_ssh_connection.py

T4: the fake SSH connection must satisfy exactly the asyncssh surface
CommandExecutor touches, including the protocol-significant stderr reads.

Getting these wrong does not produce a clean error — a missing PGID line makes
int() fail, and a missing READY line makes a logged run raise
CommandExecutionException. See docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest

from app.repositories.dry_run_ssh_connection import (
    DryRunProcess,
    DryRunSSHConnection,
)


# ── connection surface ────────────────────────────────────────────────────────


def test_exposes_the_four_members_the_executor_uses():
    conn = DryRunSSHConnection()
    for member in ("run", "create_process", "is_closed", "close"):
        assert hasattr(conn, member), f"missing {member}"


async def test_run_returns_result_with_exit_status_and_streams():
    """conn.run() feeds the version precheck and the fire-and-forget dispatch,
    both of which read exit_status / stdout / stderr."""
    result = await DryRunSSHConnection().run("echo hi")
    assert result.exit_status == 0
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)


async def test_version_probe_returns_parseable_semver():
    """The precheck runs `<script> --version` and *fails closed* if the output
    cannot be parsed, so a generic stdout body would reject every request."""
    result = await DryRunSSHConnection().run("/opt/run.sh --version")
    assert result.exit_status == 0
    assert result.stdout.strip() == "999.0.0"


def test_is_closed_is_false_until_closed():
    conn = DryRunSSHConnection()
    assert conn.is_closed() is False
    conn.close()
    assert conn.is_closed() is True


async def test_closes_after_run_for_disconnecting_commands():
    """disconnects_ssh commands (e.g. reboot) are detected via is_closed()
    after the run — this is what lets the fire-and-forget path be exercised
    without a real host ever rebooting."""
    conn = DryRunSSHConnection(closes_after_run=True)
    await conn.run("reboot")
    assert conn.is_closed() is True


async def test_connection_stays_open_for_normal_commands():
    conn = DryRunSSHConnection()
    await conn.run("ls")
    assert conn.is_closed() is False


# ── process surface ───────────────────────────────────────────────────────────


async def test_create_process_returns_process_with_expected_members():
    proc = await DryRunSSHConnection().create_process("ls")
    for member in ("stdout", "stderr", "communicate", "wait", "returncode"):
        assert hasattr(proc, member), f"missing {member}"


async def test_first_stderr_line_parses_as_a_pgid():
    """The executor does int(line) on the first stderr line. A non-numeric
    value is logged and swallowed, so the PGID would silently never be
    recorded — assert it parses."""
    proc = await DryRunSSHConnection().create_process("ls")
    first = await proc.stderr.readline()
    assert int(first.strip()) > 0


async def test_stderr_is_exhausted_after_its_queued_lines():
    """readline() must not block or loop forever once the queue is drained."""
    proc = await DryRunSSHConnection().create_process("ls")
    await proc.stderr.readline()
    assert await proc.stderr.readline() == ""


async def test_communicate_returns_stdout_and_stderr_pair():
    """_collect_output unpacks exactly two values and merges them."""
    proc = await DryRunSSHConnection().create_process("ls")
    out, err = await proc.communicate()
    assert isinstance(out, str) and isinstance(err, str)
    assert out


async def test_wait_is_awaitable():
    """_execute_pipeline awaits wait() on every process but the last."""
    proc = await DryRunSSHConnection().create_process("ls")
    assert await proc.wait() is proc


async def test_exit_status_is_configurable():
    conn = DryRunSSHConnection(exit_status=3)
    proc = await conn.create_process("false")
    assert proc.returncode == 3


async def test_custom_stderr_lines_are_served_in_order():
    """T6 needs a READY line after the PGID; the queue must preserve order."""
    conn = DryRunSSHConnection(stderr_lines=["4242\n", "READY\n"])
    proc = await conn.create_process("run.sh")
    assert (await proc.stderr.readline()).strip() == "4242"
    assert (await proc.stderr.readline()).strip() == "READY"


def test_process_can_be_constructed_directly():
    assert DryRunProcess(command="x").command == "x"


# ── kill-path behaviour (T5) ──────────────────────────────────────────────────


async def test_kill_probe_reports_process_gone():
    """`kill -0 -<pgid>` probes whether the group survived SIGTERM. Reporting
    non-zero (as a real kill does for a dead group) lets the two-phase kill
    finish at SIGTERM instead of always escalating to SIGKILL against nothing.
    """
    result = await DryRunSSHConnection().run("kill -0 -999001")
    assert result.exit_status != 0


async def test_kill_signals_are_accepted():
    for cmd in ("kill -TERM -999001", "kill -KILL -999001"):
        assert (await DryRunSSHConnection().run(cmd)).exit_status == 0


async def test_run_seconds_delays_completion():
    """Without a delay a dry-run command is terminal before a kill can arrive,
    so RUNNING → KILLING → KILLED would be unreachable."""
    import time

    conn = DryRunSSHConnection(run_seconds=0.2)
    proc = await conn.create_process("sleep 60")
    started = time.monotonic()
    await proc.communicate()
    assert time.monotonic() - started >= 0.15


async def test_zero_run_seconds_completes_immediately():
    import time

    proc = await DryRunSSHConnection().create_process("ls")
    started = time.monotonic()
    await proc.communicate()
    assert time.monotonic() - started < 0.1
