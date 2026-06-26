import asyncio
import json
import logging
import re
import uuid
import os
import shlex
import asyncssh
from typing import Dict, Any, List, Optional
from datetime import timedelta

from app.domain.command import (
    CommandExecutionRequest, CommandExecutionResponse,
    UserCommandWhitelist, CommandWhitelistConfig,
    SSHConnectionConfig, RunningCommandEntry, ExecutionContext,
    CommandState, CommandStatus, HostType,
    CommandLogLine, CommandTraceResponse,
    CommandArgumentConfig,
)
from app.core.log_renderer import LogRenderer
from app.core.config import get_settings
from app.core.redis_client import RedisClient
from app.repositories.ssh_auth_repository import create_authenticator
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.host_resolver import ResolvedHost, create_host_resolver
from app.services.pipeline_builder import PipelineBuilder
from app.services.command_ssh import SshSupport
from app.services.command_state_helpers import StateHelpers
from app.services.command_trace import CommandTrace
from app.core.exceptions import (
    CommandExecutionException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
    ForbiddenException,
    NotFoundException,
    ServiceUnavailableException,
    BaseAppException,
)

logger = logging.getLogger(__name__)
settings = get_settings()

from app.services.command_pool import (  # noqa: F401  (re-exported for callers/tests)
    pool_add, pool_get, pool_remove, pool_size, pool_command_ids,
    _get_semaphore,
)


def _decode(stream: Any) -> str:
    """Normalise an asyncssh stdout/stderr stream (bytes | str | None) to str."""
    if not stream:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8")
    return str(stream)


class CommandService:
    def __init__(
        self,
        repo: CommandStateRepository,
        inventory_repo: Optional[InventoryRepository] = None,
    ):
        self.repo = repo
        self.inventory_repo = inventory_repo
        self._pipeline_builder = PipelineBuilder()
        self._ssh = SshSupport()
        self._state = StateHelpers(repo=self.repo, ssh=self._ssh)
        self._trace = CommandTrace(state=self._state, ssh=self._ssh)

    def _validate_anti_injection(self, user_input: str):
        """Early-rejection layer: block inputs containing shell meta-characters.

        This is the first of three anti-injection defences (see ssh-command.md §4).
        Even if this layer is bypassed, the shlex positional-argument architecture
        guarantees that user values are never evaluated as shell syntax.

        Raises:
            CommandExecutionException: If any dangerous character is detected.
        """
        dangerous_chars = [";", "&", "|", "$", "`"]
        if any(char in user_input for char in dangerous_chars):
            raise CommandExecutionException("Invalid characters detected in input.")

    def _load_user_whitelist(self, username: str) -> UserCommandWhitelist:
        """Load the command whitelist configuration for a given user role.

        Reads ``data/allow-commands-{username}.json`` and deserialises it
        into a ``UserCommandWhitelist`` model.

        Raises:
            ForbiddenException: If the configuration file does not exist.
        """
        file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"allow-commands-{username}.json")
        if not os.path.exists(file_path):
            raise ForbiddenException(
                f"User '{username}' has no command whitelist configured.",
                detail={"username": username},
            )
        with open(file_path, "r") as f:
            data = json.load(f)
        return UserCommandWhitelist(**data)

    async def get_command_execution_result(self, command_id: str) -> CommandExecutionResponse:
        """Poll the current status / result for a previously submitted command from Redis.

        Orphan-run recovery: if Redis still says RUNNING for a ``logged``
        command, the originating pod may have died mid-run while
        ``run-ansible.sh`` kept going on the control_node. We SSH back, read the
        ``<run_id>.exit`` marker (the log is the source of truth), and lazily
        heal the state. This makes the asyncio.Task (fast path) an optimisation,
        not the only writer of the final result.

        Raises:
            NotFoundException: If the command_id does not exist in Redis.
        """
        state = await self._state._get_state_or_404(command_id)

        # Heal stuck transient states (RUNNING, or KILLING that never resolved —
        # e.g. a non-killable run flipped to KILLING on shutdown, or the killing
        # pod died) from the control_node marker. Terminal states are left alone.
        if state.status in (CommandStatus.RUNNING, CommandStatus.KILLING) and state.run_log_path:
            state = await self._state._heal_from_marker(state)

        return CommandExecutionResponse(
            status=state.status,
            command_id=state.command_id,
            exit_status=state.exit_code,
            output=state.output,
            message=state.message or "",
            exec_command=state.exec_command,
            host_type=state.host_type,
            resolved_ip=state.resolved_ip,
            pgids=state.pgids,
        )

    async def get_command_trace(self, command_id: str, byte_offset: int = 0, line_num: int = 1) -> CommandTraceResponse:
        return await self._trace.get_command_trace(command_id, byte_offset, line_num)

    def get_user_commands(self, username: str) -> UserCommandWhitelist:
        """Return the full command whitelist available to the given user."""
        return self._load_user_whitelist(username)

    def get_command_info(self, username: str, command_name: str) -> CommandWhitelistConfig:
        """Return the whitelist definition for a single command.

        Raises:
            CommandExecutionException: If command_name is not in the user's whitelist.
        """
        whitelist = self._load_user_whitelist(username)
        cmd_config = next((c for c in whitelist.allow_commands if c.command_name == command_name), None)
        if not cmd_config:
            raise CommandExecutionException(f"Command '{command_name}' not found.")
        return cmd_config

    async def _prepare_execution(
        self, username: str, request_id: str, req: CommandExecutionRequest,
    ) -> ExecutionContext:
        """Validate the incoming request and assemble an ExecutionContext.

        Performs whitelist lookup, host resolution, allow/deny matching
        on the resolved IP, argument validation (anti-injection + regex),
        and SSH config loading. All results are bundled into an
        ``ExecutionContext`` dataclass for downstream consumption.

        Raises:
            ForbiddenException: Missing whitelist, host blocked/not allowed,
                command not in whitelist.
            NotFoundException: Hostname not found in inventory (propagated).
            UpstreamTimeoutException / UpstreamUnavailableException:
                Inventory lookup failures (propagated).
            CommandExecutionException: Argument validation failures (400).
            BaseAppException: SSH configuration missing on disk (500).
        """
        whitelist = self._load_user_whitelist(username)

        bastion_type = (
            req.option.bastion_type
            if req.option and req.option.bastion_type
            else None
        )
        ip_label = (
            req.option.ip_label if req.option and req.option.ip_label
            else settings.INVENTORY_IP_LABEL
        )
        resolver = create_host_resolver(
            req.host_type,
            inventory_repo=self.inventory_repo,
            node_type_map=settings.BASTION_NODE_TYPE_MAP,
            bastion_type=bastion_type,
            ip_label=ip_label,
            slash_map=settings.CLUSTER_SLASH_TYPE_MAP,
        )
        resolved = await resolver.resolve(req.host)

        if any(re.match(pattern, resolved.ip) for pattern in whitelist.deny_hosts):
            logger.warning(
                f"Host '{resolved.ip}' is blocked for user '{username}' by deny list.",
                extra={
                    "request_id": request_id, "username": username,
                    "host": req.host, "host_type": req.host_type.value,
                    "resolved_ip": resolved.ip,
                },
            )
            raise ForbiddenException(
                f"Host '{resolved.ip}' is blocked.",
                detail={"host": req.host, "resolved_ip": resolved.ip},
            )

        if not any(re.match(pattern, resolved.ip) for pattern in whitelist.allow_hosts):
            logger.warning(
                f"Host '{resolved.ip}' is not allowed for user '{username}' by allow list.",
                extra={
                    "request_id": request_id, "username": username,
                    "host": req.host, "host_type": req.host_type.value,
                    "resolved_ip": resolved.ip,
                },
            )
            raise ForbiddenException(
                f"Host '{resolved.ip}' is not allowed.",
                detail={"host": req.host, "resolved_ip": resolved.ip},
            )

        cmd_config = next(
            (c for c in whitelist.allow_commands if c.command_name == req.command_name),
            None,
        )
        if not cmd_config:
            raise ForbiddenException(
                f"Command '{req.command_name}' not in user '{username}' whitelist.",
                detail={"command_name": req.command_name, "username": username},
            )

        for arg_conf in cmd_config.arguments:
            val = req.arguments.get(arg_conf.name)
            if val is None:
                if not arg_conf.required:
                    continue  # optional and omitted — skip; pipeline drops its tokens
                raise CommandExecutionException(
                    f"Missing required argument: {arg_conf.name}",
                    detail={"argument": arg_conf.name},
                )
            val_str = str(val)
            self._validate_anti_injection(val_str)
            if arg_conf.validation_regex:
                if not re.match(arg_conf.validation_regex, val_str):
                    raise CommandExecutionException(
                        f"Argument '{arg_conf.name}' does not match validation regex.",
                        detail={"argument": arg_conf.name},
                    )

        ssh_config = self._ssh._load_ssh_config(req.ssh_config)

        return ExecutionContext(
            username=username,
            request_id=request_id,
            command_name=req.command_name,
            raw_request=req,
            cmd_config=cmd_config,
            ssh_config=ssh_config,
            resolved_host=resolved,
        )

    def _compute_log_path(self, command_id: str) -> str:
        """Control_node path where run-ansible.sh tees this run's log."""
        return f"{settings.COMMAND_LOG_DIR}/{command_id}.log"

    async def _connect(self, context: ExecutionContext, req: CommandExecutionRequest) -> asyncssh.SSHClientConnection:
        """Establish an SSH connection to the target host.

        Delegates credential handling to the authenticator resolved from
        ``context.ssh_config`` (key-based or certificate-based).
        Connection attempt is bounded by ``SSH_CONNECT_TIMEOUT_SECONDS``.

        Raises:
            UpstreamTimeoutException:    Connect exceeded the configured timeout (504).
            UpstreamUnavailableException: Host unreachable, DNS failure, auth rejected,
                                          or other connect-time failure (502).
        """
        authenticator = create_authenticator(context.ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()
        ip = context.resolved_host.ip
        target = f"{ip}:{req.port}"
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host=ip,
                    port=req.port,
                    username=req.username,
                    **conn_kwargs,
                ),
                timeout=settings.SSH_CONNECT_TIMEOUT_SECONDS,
            )
            return conn
        except asyncio.TimeoutError as exc:
            raise UpstreamTimeoutException(
                f"SSH connection to {target} (host_type={req.host_type.value}, raw={req.host}) "
                f"timed out after {settings.SSH_CONNECT_TIMEOUT_SECONDS}s.",
                detail={
                    "host": req.host, "host_type": req.host_type.value,
                    "resolved_ip": ip, "port": req.port,
                },
            ) from exc
        except (OSError, asyncssh.Error) as exc:
            # OSError covers DNS failure / ECONNREFUSED / network-unreachable.
            # asyncssh.Error covers protocol-level failures (auth, host-key
            # mismatch, disconnect during handshake). Neither is a client
            # input problem — the upstream is the failing party.
            raise UpstreamUnavailableException(
                f"SSH connection to {target} (host_type={req.host_type.value}, raw={req.host}) failed: {exc}",
                detail={
                    "host": req.host, "host_type": req.host_type.value,
                    "resolved_ip": ip, "port": req.port,
                },
            ) from exc

    async def _handle_fire_and_forget(self, context: ExecutionContext) -> CommandExecutionResponse:
        """Execute a command that is expected to sever the SSH connection (e.g. reboot).

        Uses dual-mode detection:
          1. Run the command and wait for it to finish.
          2. After completion, check ``conn.is_closed()``.
             - Closed → the remote host disconnected as expected → ``success``.
             - Still open → the command completed without severing → ``failed``
               with stdout/stderr so the caller can see why.
          3. If ``ConnectionLost`` is raised mid-execution → ``success``.
        """
        conn = context.conn
        target = f"{context.raw_request.host}:{context.raw_request.port}"
        log_extra = {
            "request_id": context.request_id, "username": context.username,
            "host": context.raw_request.host, "port": context.raw_request.port,
        }
        try:
            cmd_line = context.pipeline_cmds[0]
            cmd_str_preview = shlex.join(cmd_line)
            logger.info(
                f"Dispatching fire-and-forget command '{context.command_name}' to {target}: {cmd_str_preview}",
                extra=log_extra,
            )
            
            result = await conn.run(cmd_str_preview, check=False)

            # After the command returns, check whether the connection was severed.
            if conn.is_closed():
                logger.info(
                    f"Connection to {target} closed after '{context.command_name}' — expected behaviour.",
                    extra=log_extra,
                )
                return CommandExecutionResponse(
                    status=CommandStatus.SUCCESS.value,
                    message=f"Command dispatched and connection to {target} dropped as expected.",
                    exec_command=cmd_str_preview,
                )
            
            # Connection is still alive — the command did NOT cause a disconnect.
            out_str = _decode(result.stdout)
            err_str = _decode(result.stderr)
            final_output = out_str + ("\n" + err_str if err_str and out_str else err_str)
                
            logger.warning(
                f"Fire-and-forget command '{context.command_name}' on {target} completed without disconnecting.",
                extra=log_extra,
            )
            return CommandExecutionResponse.failed(
                message="Command executed but did not disconnect the session as expected.",
                exit_status=result.exit_status,
                output=final_output,
            )
        except asyncssh.ConnectionLost:
            logger.info(
                f"Connection to {target} dropped as expected during '{context.command_name}'.",
                extra=log_extra,
            )
            return CommandExecutionResponse(
                status=CommandStatus.SUCCESS.value,
                message=f"Command dispatched and connection to {target} dropped as expected.",
            )
        except Exception as e:
            logger.error(
                f"Unexpected error during fire-and-forget '{context.command_name}' on {target}: {str(e)}",
                extra=log_extra,
            )
            return CommandExecutionResponse.failed(message=f"Unexpected error: {str(e)}")
        finally:
            conn.close()

    def _apply_output_policy(self, logged: bool, success: bool, output: str) -> Optional[str]:
        """Decide what output to persist on CommandState for a finished command.

        Non-logged commands keep their full output (legacy behaviour). Logged
        commands rely on the control_node file + /view for the full log, so we
        persist nothing on success and only a short failure tail on failure.
        """
        if not logged:
            return output
        if success:
            return None
        tail_lines = settings.COMMAND_LOG_FAILURE_TAIL_LINES
        if tail_lines <= 0:
            return None
        if not output:
            return None
        return "\n".join(output.split("\n")[-tail_lines:])

    async def _store_result(self, command_id: str, response: CommandExecutionResponse):
        """Persist a finished command's response into the existing Redis state machine with a new TTL."""
        ttl = settings.COMMAND_RESULT_TTL_SECONDS
        
        async def updater(state: CommandState):
            if response.status == CommandStatus.SUCCESS.value:
                state.mark_success(response.exit_status or 0, response.output or "")
            else:
                state.mark_failed(response.message or "", exit_code=response.exit_status, output=response.output)
        
        # SAFETY: Only update outcome if the current state is still RUNNING.
        # If it's KILLING or KILLED, it means the command was aborted or killed externally.
        updated = await self.repo.update_if(
            command_id,
            condition=lambda s: s.status == CommandStatus.RUNNING,
            updater=updater,
            ttl_seconds=ttl
        )
        if not updated:
            logger.info(f"Skipping result storage for {command_id}; state was not RUNNING (possibly killed).")

    def _build_step_wrapper(self, run_log_path: Optional[str]) -> List[str]:
        """Build the ``setsid ... sh -c <script> _`` wrapper for one pipeline step.

        ANTI-INJECTION ARCHITECTURE NOTE (unchanged):
        User arguments are appended as positional args to ``sh -c`` and consumed
        via ``"$@"``; ``shlex.join`` quotes them so ``sh`` treats them as literal
        strings, immune to ``$()`` / ``\\n`` expansion. The ``sh -c`` exists to
        emit the PGID (``echo $$``) for precise lifecycle/timeout kills.

        Two modes:
        * ``run_log_path is None`` (non-logged): output streams back over the SSH
          channel (``asyncssh.PIPE``), as before — these commands are short and
          their output IS the result.
        * ``run_log_path`` set (logged, e.g. ansible): the run's stdout/stderr
          are severed from the SSH channel (redirected to ``/dev/null``) and
          stdin detached (``< /dev/null``). This is what lets the run SURVIVE
          deploy-service going away — its output no longer flows through the SSH
          channel, so closing that channel can't SIGPIPE-cascade the process to
          death (the exit-141 bug). The run script itself ``tee``s its output to
          the control_node log file, and the viewer/heal read that file over a
          separate SSH connection, so nothing is lost. (We redirect to
          ``/dev/null`` rather than the log file precisely so we don't
          double-write the file the script already owns.)

          A two-line stderr handshake is emitted BEFORE exec (so it reaches the
          channel even though exec's output is redirected): the PGID, then a
          literal ``READY``. ``_execute_pipeline`` waits for ``READY`` to confirm
          the command actually reached exec — if it never arrives (script not
          found, log dir unwritable, etc.) the run is a start-up failure and is
          reported as such instead of hanging in RUNNING (blind-spot B).
        """
        if not run_log_path:
            return ["setsid", "-w", "sh", "-c", 'echo $$ >&2; exec "$@"', "_"]
        script = (
            'echo $$ >&2; echo READY >&2; '
            'exec "$@" > /dev/null 2>&1 < /dev/null'
        )
        return ["setsid", "-w", "sh", "-c", script, "_"]

    async def _execute_pipeline(self, context: ExecutionContext, command_id: str, cmd_str_preview: str):
        """Spawn each pipeline step on the remote host and capture PGIDs.

        Each step is wrapped (see ``_build_step_wrapper``) to create an isolated
        process group whose PGID can be used for precise timeout kills. Steps are
        chained via Python-side stdin/stdout piping (not shell ``|``) to prevent
        pipe-based injection. For logged commands the wrapper detaches the run's
        output to the control_node log file so it survives a channel close.

        Returns:
            The final ``asyncssh.SSHClientProcess`` whose output should be
            collected by ``_collect_output``.
        """
        entry = pool_get(command_id)
        if not entry:
            return 1, ""

        # Logged commands detach their output to the log file (survive channel
        # close); non-logged stream back as before.
        detached = bool(context.run_log_path)
        processes = []
        pgids = []
        prev_stdout = None

        for i, cmd_args in enumerate(context.pipeline_cmds):
            wrapper = self._build_step_wrapper(context.run_log_path if detached else None)
            full_cmd = wrapper + cmd_args

            try:
                command_str = shlex.join(full_cmd)
                p = await context.conn.create_process(
                    command_str,
                    stdin=prev_stdout,
                    stdout=asyncssh.PIPE,
                    stderr=asyncssh.PIPE
                )
            except Exception as e:
                logger.error(f"Failed to create process: {e}", extra={"request_id": context.request_id, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port})
                raise

            processes.append(p)
            pgid_str = await p.stderr.readline()
            if pgid_str:
                try:
                    pgids.append(int(pgid_str.strip()))
                except Exception:
                    logger.error(f"Could not parse PGID from: {pgid_str}", extra={"request_id": context.request_id, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port})

            # Blind-spot B: for a detached (logged) run, the run's own output no
            # longer comes back over the channel, so the ONLY way to know it
            # actually started is the READY handshake line. If it's missing the
            # command died before exec (script not found, log dir unwritable) —
            # fail fast instead of leaving the run hung in RUNNING.
            if detached:
                ready = await p.stderr.readline()
                if (ready or "").strip() != "READY":
                    raise CommandExecutionException(
                        "Run failed to start on the control_node "
                        "(no READY handshake; check the run script path and log dir).",
                        detail={"command_id": command_id},
                    )

            prev_stdout = p.stdout

        entry.processes = processes
        entry.pgids = pgids
        
        logger.info(
            f"Command '{context.command_name}' ({cmd_str_preview}) PGIDs assigned: {pgids}",
            extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
        )

        for p in processes[:-1]:
            await p.wait()

        return processes[-1]

    async def _collect_output(self, final_process: asyncssh.SSHClientProcess) -> tuple[int, str]:
        """Drain stdout and stderr from the final pipeline process.

        Merges both streams into a single output string (stdout first,
        stderr appended with a newline separator when both are non-empty).

        Returns:
            A tuple of ``(exit_code, merged_output_string)``.
        """
        stdout_data, stderr_data = await final_process.communicate()
        out_str = _decode(stdout_data)
        err_str = _decode(stderr_data)

        final_output = out_str + ("\n" + err_str if err_str and out_str else err_str)
        return final_process.returncode, final_output

    async def _handle_async_execution(self, context: ExecutionContext, command_id: Optional[str] = None) -> CommandExecutionResponse:
        """Register a background task for pipeline execution with timeout control.

        Immediately returns ``status: running`` with a ``command_id``.
        The actual work runs inside an ``asyncio.Task`` that:
          1. Executes the pipeline (``_execute_pipeline``).
          2. Collects output (``_collect_output``).
          3. Stores the result (``_store_result``).
        On timeout, triggers the two-phase kill (SIGTERM → SIGKILL)
        via ``kill_command``.

        ``command_id`` may be pre-generated by ``execute_command`` (for
        ``logged`` commands, whose id must be known before the pipeline is
        built so ``{run_id}`` can resolve); otherwise one is generated here.
        """
        command_id = command_id or str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        
        entry = RunningCommandEntry(
            host_ip=context.resolved_host.ip,
            killable=context.cmd_config.killable,
            conn=context.conn,
        )
        pool_add(command_id, entry)

        opt = context.raw_request.option
        timeout_seconds = (
            opt.timeout_seconds if opt is not None and opt.timeout_seconds is not None
            else settings.COMMAND_DEFAULT_TIMEOUT
        )

        cmd_str_preview = " | ".join(shlex.join(cmd) for cmd in context.pipeline_cmds)

        state = CommandState(
            command_id=command_id,
            status=CommandStatus.RUNNING,
            host=context.raw_request.host,
            host_type=context.raw_request.host_type,
            resolved_ip=context.resolved_host.ip,
            port=context.raw_request.port,
            # SSH account from the request (e.g. root), NOT the deploy-service
            # login account (context.username). Cross-pod kill and the log
            # viewer reconnect over SSH using state.username.
            username=context.raw_request.username,
            ssh_config=context.raw_request.ssh_config,
            request_id=context.request_id,
            killable=context.cmd_config.killable,
            pgids=[],
            exec_command=cmd_str_preview,
            run_log_path=context.run_log_path,
        )
        await self.repo.save(state, timeout_seconds + 30)

        logger.info(
            f"Initiating command '{context.command_name}' ({cmd_str_preview}) to {context.raw_request.host}:{context.raw_request.port} with timeout {timeout_seconds}s.",
            extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
        )

        async def _execution_task():
            # 1. Execute Pipeline
            final_process = await self._execute_pipeline(context, command_id, cmd_str_preview)

            # Update PGIDs in Repository
            if entry.pgids:
                await self.repo.update(
                    command_id,
                    lambda s: setattr(s, "pgids", entry.pgids),
                    timeout_seconds + 30
                )

            # 2. Collect Output
            returncode, output = await self._collect_output(final_process)

            logger.info(
                f"Command '{context.command_name}' finished. Exit Status: {returncode}",
                extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
            )

            success = returncode == 0
            stored_output = self._apply_output_policy(
                context.cmd_config.logged, success, output,
            )
            if success:
                res = CommandExecutionResponse.success(command_id=command_id, exit_status=returncode, output=stored_output or "")
            else:
                res = CommandExecutionResponse.failed(message="", exit_status=returncode, output=stored_output, command_id=command_id)
            await self._store_result(command_id, res)

        async def _timeout_wrapper():
            try:
                async with _get_semaphore():
                    await asyncio.wait_for(_execution_task(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Command '{context.command_name}' timed out after {timeout_seconds}s.",
                    extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
                )
                await self.kill_command(command_id, message="Command timed out and was killed.")
            except Exception as e:
                logger.error(
                    f"Command '{context.command_name}' failed asynchronously: {str(e)}",
                    extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
                )
                await self._store_result(command_id, CommandExecutionResponse.failed(
                    message=str(e), command_id=command_id
                ))
            finally:
                context.conn.close()
                pool_remove(command_id)

        entry.task = loop.create_task(_timeout_wrapper())
        return CommandExecutionResponse(status=CommandStatus.RUNNING.value, command_id=command_id)

    def _check_capacity(self, username: str, request_id: str) -> None:
        """Raise ServiceUnavailableException if the running pool is full."""
        if pool_size() >= settings.COMMAND_MAX_RUNNING:
            logger.warning(
                f"Max running commands reached ({settings.COMMAND_MAX_RUNNING}), rejecting new request.",
                extra={"request_id": request_id, "username": username},
            )
            raise ServiceUnavailableException(
                f"Too many running commands (limit: {settings.COMMAND_MAX_RUNNING}). "
                "Please try again later.",
                detail={"max_running": settings.COMMAND_MAX_RUNNING},
            )

    async def execute_command(
        self, username: str, request_id: str, req: CommandExecutionRequest,
    ) -> CommandExecutionResponse:
        """Top-level orchestrator for SSH command execution.

        Coordinates the full lifecycle:
          1. ``_check_capacity``    — backpressure gate.
          2. ``_prepare_execution`` — validate, resolve host, build context.
          3. ``_pipeline_builder.build`` — resolve argument placeholders.
          4. ``_connect``           — establish SSH session.
          5. Route to ``_handle_fire_and_forget`` or ``_handle_async_execution``.

        Typed BaseAppException subclasses (Forbidden / NotFound / Upstream /
        ServiceUnavailable / CommandExecution / BaseAppException) are allowed
        to propagate so the global handler in main.py renders the structured
        JSON error response.
        """
        self._check_capacity(username, request_id)

        context = await self._prepare_execution(username, request_id, req)

        # For `logged` commands the command_id must exist BEFORE the pipeline is
        # built, so the server-injected `{run_id}` placeholder can resolve and
        # the tee target (`run_log_path`) is known. Reuse command_id as run_id.
        command_id = None
        if context.cmd_config.logged:
            command_id = str(uuid.uuid4())
            context.run_id = command_id
            context.run_log_path = self._compute_log_path(command_id)

        context.pipeline_cmds = self._pipeline_builder.build(context)

        conn = await self._connect(context, req)
        context.conn = conn

        if context.cmd_config.disconnects_ssh:
            return await self._handle_fire_and_forget(context)

        return await self._handle_async_execution(context, command_id=command_id)

    async def kill_command(self, command_id: str, message: str = "Killed", force: bool = False):
        """Terminate a running command using two-phase PGID-based kill.

        Phase 1: ``kill -TERM -{pgid}`` (soft kill).
        Phase 2: After a grace period, ``kill -KILL -{pgid}``.

        Transitions state: RUNNING -> KILLING -> KILLED.

        ``force`` is a HUMAN override: a user who explicitly asks to kill a
        ``killable: false`` command (via ``POST /kill?force=true``) bypasses the
        killable guard. Automatic callers — the timeout wrapper and
        ``shutdown_gracefully`` — never pass ``force``, so they keep respecting
        ``killable`` (the flag's purpose is "the system must not kill this on its
        own", not "no one may ever kill this").
        """
        ttl = settings.COMMAND_RESULT_TTL_SECONDS

        # 0. Refuse to kill a non-killable command UNLESS a human forced it. This
        # MUST happen before any state transition: flipping to KILLING and then
        # bailing strands the command in KILLING forever (it has no kill path to
        # reach KILLED). We leave it RUNNING so the marker heal can later resolve
        # its true outcome.
        if not force:
            entry = pool_get(command_id)
            if entry is not None:
                is_killable = entry.killable
            else:
                try:
                    is_killable = (await self.repo.get(command_id)).killable
                except CommandExecutionException:
                    logger.info(
                        f"Kill request for unknown command {command_id}; nothing to do.",
                        extra={"command_id": command_id},
                    )
                    return
            if not is_killable:
                logger.warning(
                    f"Command {command_id} is not killable; leaving state untouched.",
                    extra={"command_id": command_id},
                )
                return

        # 1. Atomic State Transition to KILLING (only from RUNNING).
        success = await self.repo.update_if(
            command_id,
            condition=lambda s: s.status == CommandStatus.RUNNING,
            updater=lambda s: s.mark_killing(message),
            ttl_seconds=ttl
        )

        if not success:
            logger.info(f"Kill request aborted for {command_id}: Command is not in RUNNING state.")
            return

        # 2. Perform SSH Kill Logic
        # Try Local Kill First
        entry = pool_get(command_id)
        if entry:
            await self._do_kill_via_connection(entry.conn, entry.pgids, command_id)
            await self.repo.update(command_id, lambda s: s.mark_killed(message), ttl)
            return

        # Try Cross-Pod Kill via Repository
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException:
            logger.info(
                f"Cross-pod kill for {command_id} aborted; state vanished from Redis.",
                extra={"command_id": command_id},
            )
            return

        if not state.pgids:
            # If no PGIDs yet, we've already marked it as KILLING,
            # so the async task will eventually hit _store_result and be blocked.
            await self.repo.update(command_id, lambda s: s.mark_killed(message), ttl)
            return

        logger.info(
            f"Initiating cross-pod kill for {command_id} on {state.resolved_ip}:{state.port} "
            f"(host_type={state.host_type.value}, raw={state.host})"
        )
        
        ssh_config = self._ssh._load_ssh_config(state.ssh_config)
        authenticator = create_authenticator(ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()
        
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(host=state.resolved_ip, port=state.port, username=state.username, **conn_kwargs),
                timeout=10
            )
            try:
                await self._do_kill_via_connection(conn, state.pgids, command_id)
                await self.repo.update(command_id, lambda s: s.mark_killed(message), ttl)
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Failed cross-pod kill for {command_id}: {e}", extra={"command_id": command_id})

    async def _do_kill_via_connection(self, conn: asyncssh.SSHClientConnection, pgids: List[int], command_id: str):
        for pgid in pgids:
            try:
                logger.info(f"Soft killing PGID {pgid}", extra={"command_id": command_id})
                await conn.run(f"kill -TERM -{pgid}", check=False)
                await asyncio.sleep(settings.COMMAND_KILL_GRACE_SECONDS)
                res = await conn.run(f"kill -0 -{pgid}", check=False)
                if res.exit_status == 0:
                    logger.info(f"Process {pgid} still running, hard killing it.", extra={"command_id": command_id})
                    await conn.run(f"kill -KILL -{pgid}", check=False)
            except Exception as e:
                logger.error(f"Error killing PGID {pgid}: {e}", extra={"command_id": command_id})

    async def list_running_commands(
        self, statuses: Optional[set[CommandStatus]] = None
    ) -> List[CommandState]:
        """Return command states currently in-flight across all pods.

        Defaults to non-terminal states (RUNNING + KILLING) when no explicit
        status set is given. Reads from Redis so it sees commands started on
        other pods.
        """
        if statuses is None:
            statuses = {CommandStatus.RUNNING, CommandStatus.KILLING}
        return await self.repo.list_states(statuses)

    async def shutdown_gracefully(self):
        """Kill all active commands during application shutdown.

        Called by the FastAPI lifespan handler to ensure no orphan processes
        remain on remote hosts after the API server stops.
        """
        logger.info(f"Shutting down {pool_size()} running commands gracefully.")
        tasks = [self.kill_command(cmd_id) for cmd_id in pool_command_ids()]
        if tasks:
            await asyncio.gather(*tasks)
