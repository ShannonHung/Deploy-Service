import asyncio
import json
import os
import logging
import shlex
from typing import Optional

import asyncssh

from app.domain.command import (
    FALLBACK_SSH_CONFIG,
    CommandState,
    SSHConnectionConfig,
    is_safe_ssh_config_name,
)
from app.core.config import get_settings
from app.repositories.dry_run_ssh_connection import DryRunSSHConnection
from app.repositories.ssh_auth_repository import create_authenticator
from app.core.exceptions import (
    UpstreamTimeoutException,
    UpstreamUnavailableException,
    BaseAppException,
    SSHConfigNotFoundException,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class SshSupport:
    """SSH connection + config loading shared by executor, lifecycle, trace, poll."""

    @staticmethod
    def _ssh_config_path(name: str) -> str:
        config_dir = settings.SSH_CONFIG_DIR or settings.COMMAND_CONFIG_DIR
        return os.path.join(config_dir, f"SSH-{name}.json")

    def select_ssh_config(self, requested: str, bastion_name: Optional[str]) -> str:
        """Return the name of the Effective SSH config for a new Run.

        An explicit name is used as-is. ``default`` auto-selects: the SSH
        config of the Bastion fronting the target if one is provisioned,
        otherwise the Fallback SSH config (ADR 0002). Called once, at launch;
        the result is recorded on the Run and never re-selected.

        Runs in dry-run too — only the subsequent file read is faked — so the
        Bastion-name check below stays part of the dry-run security surface.

        Raises:
            BaseAppException: the inventory returned a Bastion name that is not
                a safe file-name component (500 — upstream data error).
        """
        if requested != FALLBACK_SSH_CONFIG or bastion_name is None:
            return requested
        if not is_safe_ssh_config_name(bastion_name):
            raise BaseAppException(
                "Inventory returned an invalid bastion name.",
                detail={"bastion_name": bastion_name},
            )
        if os.path.exists(self._ssh_config_path(bastion_name)):
            return bastion_name
        logger.warning(
            "No SSH config provisioned for bastion '%s'; using the default "
            "SSH config. Add SSH-%s.json to use bastion-specific credentials.",
            bastion_name,
            bastion_name,
        )
        return FALLBACK_SSH_CONFIG

    def _load_ssh_config(self, name: str) -> SSHConnectionConfig:
        """Load the SSH config called *name*, literally — no fallback.

        Reads ``SSH-{name}.json`` from ``SSH_CONFIG_DIR`` (which falls back to
        ``COMMAND_CONFIG_DIR`` when unset) — kept separate from the command
        whitelists so the SSH configs, which carry private keys, can live on a
        Vault-backed mount.

        Choosing *which* name to load (auto-selection for ``default``) happens
        once, at launch; this loader is also what kill / trace / poll use to
        reload a Run's recorded SSH config, so it must never re-select.

        Raises:
            SSHConfigNotFoundException: a named config is not provisioned (400).
            BaseAppException: the Fallback SSH config itself is missing (500 —
                operator misconfig).

        In dry-run no SSH connection is ever opened, so a synthetic config is
        returned rather than requiring real key material on disk. That is the
        point: a dry-run instance holds no production secrets. The value is
        never used to authenticate — DryRunSSHConnection ignores it — but the
        surrounding validation in _prepare_execution still runs. See
        docs/arch/dry-run-mode.md.
        """
        if get_settings().DRY_RUN_MODE:
            return SSHConnectionConfig(auth_method="key", key_base64="")

        file_path = self._ssh_config_path(name)
        if not os.path.exists(file_path):
            if name == FALLBACK_SSH_CONFIG:
                raise BaseAppException(
                    "SSH configuration not found.",
                    detail={"ssh_config": name},
                )
            raise SSHConfigNotFoundException(
                f"SSH config '{name}' is not provisioned. "
                f"Add the SSH-{name}.json secret, or omit ssh_config to use "
                "the default.",
                detail={"ssh_config": name},
            )
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return SSHConnectionConfig(**data)

    async def _connect_to_control_node(
        self, state: CommandState
    ) -> asyncssh.SSHClientConnection:
        """Open an SSH connection back to the control_node for a stored run.

        Shared by the log viewer (``_read_remote_log``) and orphan-run recovery
        (``_read_run_exit_marker``): both rebuild the connection purely from the
        persisted ``CommandState`` (resolved_ip / port / username / ssh_config),
        which is what makes them work from any pod.

        Raises:
            UpstreamTimeoutException / UpstreamUnavailableException:
                SSH connect failure (mirrors ``_connect``).

        This is the *second* real-SSH entry point (``CommandExecutor._connect``
        is the first) and it is reached from a different pod than the one that
        started the run, so dry-run has to stub it too — otherwise cross-pod
        kill, log tailing and orphan heal would all attempt a real connection.
        See docs/arch/dry-run-mode.md.
        """
        if get_settings().DRY_RUN_MODE:
            logger.warning(
                "DRY-RUN | op=ssh.connect_control_node | host=%s:%s | "
                "no SSH connection opened",
                state.resolved_ip,
                state.port,
            )
            return DryRunSSHConnection()

        ssh_config = self._load_ssh_config(state.ssh_config)
        authenticator = create_authenticator(ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()
        try:
            return await asyncio.wait_for(
                asyncssh.connect(
                    host=state.resolved_ip,
                    port=state.port,
                    username=state.username,
                    **conn_kwargs,
                ),
                timeout=settings.SSH_CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise UpstreamTimeoutException(
                f"SSH connect to control_node timed out for {state.command_id}.",
                detail={"command_id": state.command_id},
            ) from exc
        except (OSError, asyncssh.Error) as exc:
            raise UpstreamUnavailableException(
                f"SSH connect to control_node failed for {state.command_id}: {exc}",
                detail={"command_id": state.command_id},
            ) from exc

    async def _read_log_tail(self, state: CommandState, n: int) -> Optional[str]:
        """Read the last ``n`` lines of a run's control_node log over SSH.

        Used to backfill the API ``output`` for a failed ``logged`` command,
        whose SSH channel carries no output (the run redirects stdout/stderr to
        ``/dev/null`` on the target so it survives the pod dying — the real
        output lives only in ``run_log_path`` on the control_node).

        Returns the tail text, or ``None`` when there is no log path, the file
        is absent/empty, or SSH fails. SSH failures are swallowed on purpose: a
        transient control_node outage must never turn a poll into a 5xx — the
        caller keeps its last-known ``output``.
        """
        if not state.run_log_path:
            return None
        try:
            conn = await self._connect_to_control_node(state)
        except BaseAppException as exc:
            logger.info(
                f"Log-tail read failed to connect for {state.command_id}: {exc}",
                extra={"command_id": state.command_id},
            )
            return None
        try:
            quoted = shlex.quote(state.run_log_path)
            res = await conn.run(f"tail -n {int(n)} {quoted}", check=False)
            if res.exit_status != 0:
                return None  # file absent / unreadable
            text = str(res.stdout) if res.stdout else ""
            return text or None
        except (OSError, asyncssh.Error) as exc:
            # The channel can still drop mid-tail (control_node reboot, network
            # blip). Same contract as the connect failure above: a best-effort
            # backfill must never turn a poll into a 5xx.
            logger.info(
                f"Log-tail read failed for {state.command_id}: {exc}",
                extra={"command_id": state.command_id},
            )
            return None
        finally:
            conn.close()
