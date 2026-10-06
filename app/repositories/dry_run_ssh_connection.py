"""
app/repositories/dry_run_ssh_connection.py

A stand-in for ``asyncssh.SSHClientConnection`` used when ``DRY_RUN_MODE=true``.

Why this seam. ``CommandExecutor.execute_command`` runs in a fixed order:

    1. _check_capacity        — backpressure gate
    2. _prepare_execution     — whitelist load, argument regex, anti-injection,
                                host resolution
    3. _pipeline_builder.build — List[List[str]] construction
    4. _connect               — SSH session          ← only this is replaced
    5. _handle_fire_and_forget / _handle_async_execution

Every validation and security check lives in steps 1–3, strictly *before* the
connection. Substituting only step 4's return value therefore preserves the
whole security boundary — the per-user whitelist, the argument regexes, the
anti-injection check and the positional-argument construction all still run for
real. A seam any higher (the router, or the service entry) would bypass exactly
the checks an e2e test most needs to cover, while still answering 200.

**This depends on validation staying ahead of connection.** If a future change
moves validation after ``_connect``, or moves ``_connect`` earlier, dry-run
silently becomes a validation bypass. See docs/arch/dry-run-mode.md.

The surface below is exactly what the executor touches: ``run``,
``create_process``, ``is_closed`` and ``close`` on the connection, plus
``stdout`` / ``stderr`` / ``communicate`` / ``wait`` / ``returncode`` on the
process. Two stderr reads are protocol-significant and are documented at
``_DryRunStream``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

_logger = logging.getLogger(__name__)

# A plausible process-group id. The executor parses the first stderr line with
# int(), so this must look like one; it is never used to signal a real process.
_DRY_RUN_PGID = 999001

_DRY_RUN_STDOUT = "[dry-run] command was not executed on any host\n"


class _DryRunResult:
    """Return value of ``conn.run()`` — mirrors asyncssh's completed-process."""

    def __init__(
        self,
        stdout: str = _DRY_RUN_STDOUT,
        stderr: str = "",
        exit_status: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status
        self.returncode = exit_status


class _DryRunStream:
    """A readable stream backing a fake process's stdout / stderr.

    ``readline()`` serves a queued list of lines and then returns ``""``
    forever, which is what the executor's two protocol-significant reads need:

      1. The **first** stderr line is parsed as a PGID via ``int()``.
      2. For a ``detached`` (``logged``) run, the **second** stderr line must be
         exactly ``READY`` or the executor raises CommandExecutionException
         ("Run failed to start on the control_node").

    T4 only needs the PGID line; the READY handshake is wired in T6, which is
    why ``lines`` is parameterised rather than hardcoded.
    """

    def __init__(self, lines: Optional[list[str]] = None, body: str = "") -> None:
        self._lines = list(lines or [])
        self._body = body

    async def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        return ""

    async def read(self, _n: int = -1) -> str:
        body, self._body = self._body, ""
        return body


class DryRunProcess:
    """Stand-in for ``asyncssh.SSHClientProcess``.

    *run_seconds* makes the fake run take measurable time. A dry-run command
    otherwise completes the instant it starts, which means it has already
    reached a terminal state before a kill request can arrive — so the
    RUNNING → KILLING → KILLED path could never be exercised. The delay is
    applied in ``communicate()``, the point where the executor waits for the
    run to finish.
    """

    def __init__(
        self,
        command: str = "",
        stdout_body: str = _DRY_RUN_STDOUT,
        stderr_lines: Optional[list[str]] = None,
        exit_status: int = 0,
        run_seconds: float = 0.0,
    ) -> None:
        self.command = command
        self.returncode = exit_status
        self.exit_status = exit_status
        # The PGID line is always present: the executor reads it for every run.
        self.stderr = _DryRunStream(lines=stderr_lines or [f"{_DRY_RUN_PGID}\n"])
        self.stdout = _DryRunStream(body=stdout_body)
        self._stdout_body = stdout_body
        self._run_seconds = run_seconds

    async def communicate(self) -> tuple[str, str]:
        """Drain both streams. ``_collect_output`` merges the pair."""
        if self._run_seconds:
            await asyncio.sleep(self._run_seconds)
        return self._stdout_body, ""

    async def wait(self) -> "DryRunProcess":
        return self

    def close(self) -> None:  # pragma: no cover - parity with asyncssh
        return None


class DryRunSSHConnection:
    """Stand-in for ``asyncssh.SSHClientConnection``.

    ``disconnects_ssh`` commands (e.g. reboot) are detected by the executor via
    ``is_closed()`` after the run, so *closes_after_run* lets a fire-and-forget
    command take its real code path without a real host ever disconnecting.
    """

    def __init__(
        self,
        *,
        closes_after_run: bool = False,
        stdout_body: str = _DRY_RUN_STDOUT,
        exit_status: int = 0,
        stderr_lines: Optional[list[str]] = None,
        run_seconds: float = 0.0,
    ) -> None:
        self._closes_after_run = closes_after_run
        self._stdout_body = stdout_body
        self._exit_status = exit_status
        self._stderr_lines = stderr_lines
        self._run_seconds = run_seconds
        self._closed = False

    async def run(self, command: str, check: bool = False, **_kwargs: Any):
        """Used by the version precheck and the fire-and-forget dispatch."""
        _logger.warning(
            "DRY-RUN | op=ssh.run | command=%s | nothing was executed", command
        )
        # The version precheck runs `<script> --version` and parses the output;
        # a plain stdout body would fail to parse and reject the request.
        if "--version" in command:
            return _DryRunResult(stdout="999.0.0\n")

        # `kill -0 -<pgid>` probes whether the group still exists between the
        # TERM and KILL phases. Reporting "gone" (non-zero, as the real kill
        # does for a dead group) lets the two-phase kill finish at SIGTERM
        # instead of always escalating to SIGKILL against nothing.
        if command.startswith("kill -0 "):
            return _DryRunResult(stdout="", exit_status=1)

        if self._closes_after_run:
            self._closed = True
        return _DryRunResult(
            stdout=self._stdout_body, exit_status=self._exit_status
        )

    async def create_process(self, command: str, **_kwargs: Any) -> DryRunProcess:
        """Used by the pipeline executor for every non-fire-and-forget run."""
        _logger.warning(
            "DRY-RUN | op=ssh.create_process | command=%s | nothing was executed",
            command,
        )
        return DryRunProcess(
            command=command,
            stdout_body=self._stdout_body,
            stderr_lines=list(self._stderr_lines) if self._stderr_lines else None,
            exit_status=self._exit_status,
            run_seconds=self._run_seconds,
        )

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:  # pragma: no cover - parity with asyncssh
        self._closed = True
