import asyncio
import json
import os
import logging

import asyncssh

from app.domain.command import SSHConnectionConfig, CommandState
from app.core.config import get_settings
from app.repositories.ssh_auth_repository import create_authenticator
from app.core.exceptions import (
    UpstreamTimeoutException,
    UpstreamUnavailableException,
    BaseAppException,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class SshSupport:
    """SSH connection + config loading shared by executor, lifecycle, trace, poll."""

    def _load_ssh_config(self, target: str) -> SSHConnectionConfig:
        """Load SSH connection configuration for the specified target cluster.

        Looks for ``data/SSH-{target}.json`` first; falls back to
        ``data/SSH-default.json`` if the target-specific file is absent.

        Raises:
            BaseAppException: If neither file exists (500 — operator misconfig).
        """
        file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"SSH-{target}.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(settings.COMMAND_CONFIG_DIR, "SSH-default.json")
            if not os.path.exists(file_path):
                raise BaseAppException(
                    "SSH configuration not found.",
                    detail={"target": target},
                )
        with open(file_path, "r") as f:
            data = json.load(f)
        return SSHConnectionConfig(**data)

    async def _connect_to_control_node(self, state: CommandState) -> asyncssh.SSHClientConnection:
        """Open an SSH connection back to the control_node for a stored run.

        Shared by the log viewer (``_read_remote_log``) and orphan-run recovery
        (``_read_run_exit_marker``): both rebuild the connection purely from the
        persisted ``CommandState`` (resolved_ip / port / username / ssh_config),
        which is what makes them work from any pod.

        Raises:
            UpstreamTimeoutException / UpstreamUnavailableException:
                SSH connect failure (mirrors ``_connect``).
        """
        ssh_config = self._load_ssh_config(state.ssh_config)
        authenticator = create_authenticator(ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()
        try:
            return await asyncio.wait_for(
                asyncssh.connect(
                    host=state.resolved_ip, port=state.port,
                    username=state.username, **conn_kwargs,
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
