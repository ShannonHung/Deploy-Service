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
    CommandState, CommandStatus,
)
from app.core.config import get_settings
from app.core.redis_client import RedisClient
from app.repositories.ssh_auth_repository import create_authenticator
from app.repositories.command_state_repository import CommandStateRepository
from app.core.exceptions import CommandExecutionException

logger = logging.getLogger(__name__)
settings = get_settings()

_local_running_commands: Dict[str, RunningCommandEntry] = {}

_execution_semaphore: Optional[asyncio.Semaphore] = None

def _get_semaphore() -> asyncio.Semaphore:
    """Lazily initialise the concurrency semaphore (must be called inside a running event loop)."""
    global _execution_semaphore
    if _execution_semaphore is None:
        _execution_semaphore = asyncio.Semaphore(settings.COMMAND_MAX_CONCURRENCY)
    return _execution_semaphore


class CommandService:
    def __init__(self, repo: CommandStateRepository):
        self.repo = repo

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
            CommandExecutionException: If the configuration file does not exist.
        """
        file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"allow-commands-{username}.json")
        if not os.path.exists(file_path):
            raise CommandExecutionException("User whitelist configuration not found. No permission.")
        with open(file_path, "r") as f:
            data = json.load(f)
        return UserCommandWhitelist(**data)

    def _load_ssh_config(self, target: str) -> SSHConnectionConfig:
        """Load SSH connection configuration for the specified target cluster.

        Looks for ``data/SSH-{target}.json`` first; falls back to
        ``data/SSH-default.json`` if the target-specific file is absent.

        Raises:
            CommandExecutionException: If neither file exists.
        """
        file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"SSH-{target}.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(settings.COMMAND_CONFIG_DIR, "SSH-default.json")
            if not os.path.exists(file_path):
                 raise CommandExecutionException("SSH configuration not found.")
        with open(file_path, "r") as f:
            data = json.load(f)
        return SSHConnectionConfig(**data)

    async def get_command_execution_result(self, command_id: str) -> CommandExecutionResponse:
        """Poll the current status / result for a previously submitted command from Redis.
        """
        state = await self.repo.get(command_id)
        return CommandExecutionResponse(
            status=state.status,
            command_id=state.command_id,
            exit_status=state.exit_code,
            output=state.output,
            message=state.message or ""
        )

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

    def _prepare_execution(self, username: str, request_id: str, req: CommandExecutionRequest) -> ExecutionContext:
        """Validate the incoming request and assemble an ExecutionContext.

        Performs whitelist lookup, argument validation (anti-injection +
        regex), and SSH config loading.  All results are bundled into an
        ``ExecutionContext`` dataclass for downstream consumption.

        Raises:
            CommandExecutionException: On any validation or config failure.
        """
        whitelist = self._load_user_whitelist(username)
        
        # Check host against deny list (blacklist)
        if any(re.match(pattern, req.host) for pattern in whitelist.deny_hosts):
            logger.warning(
                f"Host '{req.host}' is blocked for user '{username}' by deny list.",
                extra={"request_id": request_id, "username": username, "host": req.host}
            )
            raise CommandExecutionException(f"Host '{req.host}' is blocked.")

        # Check host against allow list (whitelist)
        if not any(re.match(pattern, req.host) for pattern in whitelist.allow_hosts):
            logger.warning(
                f"Host '{req.host}' is not allowed for user '{username}' by allow list.",
                extra={"request_id": request_id, "username": username, "host": req.host}
            )
            raise CommandExecutionException(f"Host '{req.host}' is not allowed.")
        
        cmd_config = next((c for c in whitelist.allow_commands if c.command_name == req.command_name), None)
        if not cmd_config:
            raise CommandExecutionException(f"Command '{req.command_name}' not found in whitelist.")

        for arg_conf in cmd_config.arguments:
            val = req.arguments.get(arg_conf.name)
            if val is None:
                raise CommandExecutionException(f"Missing required argument: {arg_conf.name}")
            val_str = str(val)
            self._validate_anti_injection(val_str)
            if arg_conf.validation_regex:
                if not re.match(arg_conf.validation_regex, val_str):
                    raise CommandExecutionException(f"Argument '{arg_conf.name}' does not match validation regex.")
        
        ssh_config = self._load_ssh_config(req.ssh_config)
        
        return ExecutionContext(
            username=username,
            request_id=request_id,
            command_name=req.command_name,
            raw_request=req,
            cmd_config=cmd_config,
            ssh_config=ssh_config
        )

    def _resolve_command_part(self, part: str, arguments: Dict[str, Any], arg_defs: list) -> str:
        """Replace {placeholder} tokens in a single command part with actual argument values."""
        for arg in arg_defs:
            placeholder = f"{{{arg.name}}}"
            if placeholder in part:
                part = part.replace(placeholder, str(arguments[arg.name]))
        return part

    def _build_pipeline(self, context: ExecutionContext) -> List[List[str]]:
        """Resolve all {placeholder} tokens and return the final pipeline.

        Pure function: produces ``List[List[str]]`` with no side-effects or
        I/O, making it trivially unit-testable.

        Returns:
            A list of command arrays, e.g. ``[["ls", "-al"], ["grep", "ssh"]]``.
        """
        return [
            [
                self._resolve_command_part(part, context.raw_request.arguments, context.cmd_config.arguments)
                for part in step.command
            ]
            for step in context.cmd_config.pipeline
        ]

    async def _connect(self, context: ExecutionContext, req: CommandExecutionRequest) -> asyncssh.SSHClientConnection:
        """Establish an SSH connection to the target host.

        Delegates credential handling to the authenticator resolved from
        ``context.ssh_config`` (key-based or certificate-based).
        Connection attempt is bounded by ``SSH_CONNECT_TIMEOUT_SECONDS``.

        Raises:
            CommandExecutionException: If the connection cannot be established or times out.
        """
        authenticator = create_authenticator(context.ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()
        target = f"{req.host}:{req.port}"
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host=req.host,
                    port=req.port,
                    username=req.username,
                    **conn_kwargs
                ),
                timeout=settings.SSH_CONNECT_TIMEOUT_SECONDS,
            )
            return conn
        except asyncio.TimeoutError:
            raise CommandExecutionException(
                f"SSH connection to {target} timed out after {settings.SSH_CONNECT_TIMEOUT_SECONDS}s."
            )
        except Exception as e:
            raise CommandExecutionException(f"SSH Connection Failed ({target}): {str(e)}")

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
                )
            
            # Connection is still alive — the command did NOT cause a disconnect.
            out_str = result.stdout if isinstance(result.stdout, str) else result.stdout.decode('utf-8') if result.stdout else ""
            err_str = result.stderr if isinstance(result.stderr, str) else result.stderr.decode('utf-8') if result.stderr else ""
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

    async def _store_result(self, command_id: str, response: CommandExecutionResponse):
        """Persist a finished command's response into the existing Redis state machine with a new TTL."""
        ttl = settings.COMMAND_RESULT_TTL_SECONDS
        
        async def updater(state: CommandState):
            if response.status == CommandStatus.SUCCESS.value:
                state.mark_success(response.exit_status or 0, response.output or "")
            else:
                state.mark_failed(response.message or "")
        
        await self.repo.update(command_id, updater, ttl)

    async def _execute_pipeline(self, context: ExecutionContext, command_id: str, cmd_str_preview: str):
        """Spawn each pipeline step on the remote host and capture PGIDs.

        Each step is wrapped with ``setsid -w sh -c 'echo $$ >&2; exec "$@"'``
        to create an isolated process group whose PGID can be used for
        precise timeout kills.  Steps are chained via Python-side stdin/stdout
        piping (not shell ``|``) to prevent pipe-based injection.

        Returns:
            The final ``asyncssh.SSHClientProcess`` whose output should be
            collected by ``_collect_output``.
        """
        entry = _local_running_commands.get(command_id)
        if not entry:
            return 1, ""

        processes = []
        pgids = []
        prev_stdout = None

        for i, cmd_args in enumerate(context.pipeline_cmds):
            # ANTI-INJECTION ARCHITECTURE NOTE:
            # We strictly pass the user arguments as positional arguments to `sh -c` using the `"$@"` array interpolation.
            # `shlex.join(full_cmd)` translates these discrete string array items into safely quoted terms before sending to SSH.
            # Example: ["grep", "val with $(rm -rf) space"] -> setsid -w sh -c '...' _ grep 'val with $(rm -rf) space'
            # Because the string is bound in single quotes natively by shlex, 'sh' treats the argument exclusively as a literal string.
            # This makes our execution mathematically immune to dynamic shell expansion escapes (like `$()`, `\n`).
            # We retain the `sh -c` purely to capture the PGID (`echo $$`) for accurate lifecycle management and timeouts.
            wrapper = ["setsid", "-w", "sh", "-c", 'echo $$ >&2; exec "$@"', "_"]
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
        out_str = stdout_data.decode('utf-8') if isinstance(stdout_data, bytes) else str(stdout_data) if stdout_data else ""
        err_str = stderr_data.decode('utf-8') if isinstance(stderr_data, bytes) else str(stderr_data) if stderr_data else ""
        
        final_output = out_str + ("\n" + err_str if err_str and out_str else err_str)
        return final_process.returncode, final_output

    async def _handle_async_execution(self, context: ExecutionContext) -> CommandExecutionResponse:
        """Register a background task for pipeline execution with timeout control.

        Immediately returns ``status: running`` with a ``command_id``.
        The actual work runs inside an ``asyncio.Task`` that:
          1. Executes the pipeline (``_execute_pipeline``).
          2. Collects output (``_collect_output``).
          3. Stores the result (``_store_result``).
        On timeout, triggers the two-phase kill (SIGTERM → SIGKILL)
        via ``kill_command``.
        """
        command_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        
        entry = RunningCommandEntry(
            host_ip=context.raw_request.host,
            killable=context.cmd_config.killable,
            conn=context.conn
        )
        _local_running_commands[command_id] = entry

        timeout_seconds = context.raw_request.option.timeout_seconds if context.raw_request.option.timeout_seconds else settings.COMMAND_DEFAULT_TIMEOUT
        
        state = CommandState(
            command_id=command_id,
            status=CommandStatus.RUNNING,
            host=context.raw_request.host,
            port=context.raw_request.port,
            username=context.username,
            ssh_config=context.raw_request.ssh_config,
            request_id=context.request_id,
            killable=context.cmd_config.killable,
            pgids=[]
        )
        await self.repo.save(state, timeout_seconds + 30)

        cmd_str_preview = " | ".join(shlex.join(cmd) for cmd in context.pipeline_cmds)
        
        logger.info(
            f"Initiating command '{context.command_name}' ({cmd_str_preview}) to {context.raw_request.host}:{context.raw_request.port} with timeout {timeout_seconds}s.",
            extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
        )

        async def _execution_task():
            try:
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
                
                if returncode == 0:
                    res = CommandExecutionResponse.success(command_id=command_id, exit_status=returncode, output=output)
                else:
                    res = CommandExecutionResponse.failed(message="", exit_status=returncode, output=output, command_id=command_id)
                await self._store_result(command_id, res)

            except Exception as e:
                # Abort safely inside task wrapper
                raise e

        async def _timeout_wrapper():
            try:
                async with _get_semaphore():
                    await asyncio.wait_for(_execution_task(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Command '{context.command_name}' timed out after {timeout_seconds}s.",
                    extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
                )
                await self.kill_command(command_id)
                await self._store_result(command_id, CommandExecutionResponse.failed(
                    message="Command timed out and was killed.", command_id=command_id
                ))
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
                _local_running_commands.pop(command_id, None)

        entry.task = loop.create_task(_timeout_wrapper())
        return CommandExecutionResponse(status=CommandStatus.RUNNING.value, command_id=command_id)

    def _check_capacity(self, username: str, request_id: str) -> Optional[CommandExecutionResponse]:
        """Return a failed response if the running pool has reached its limit, otherwise None."""
        if len(_local_running_commands) >= settings.COMMAND_MAX_RUNNING:
            logger.warning(
                f"Max running commands reached ({settings.COMMAND_MAX_RUNNING}), rejecting new request.",
                extra={"request_id": request_id, "username": username}
            )
            return CommandExecutionResponse.failed(
                f"Too many running commands (limit: {settings.COMMAND_MAX_RUNNING}). Please try again later."
            )
        return None

    async def execute_command(self, username: str, request_id: str, req: CommandExecutionRequest) -> CommandExecutionResponse:
        """Top-level orchestrator for SSH command execution.

        Coordinates the full lifecycle:
          1. ``_prepare_execution`` — validate & build context.
          2. ``_build_pipeline``    — resolve argument placeholders.
          3. ``_connect``           — establish SSH session.
          4. Route to ``_handle_fire_and_forget`` or ``_handle_async_execution``.

        Never raises; all failures are returned as ``CommandExecutionResponse.failed()``.
        """
        rejected = self._check_capacity(username, request_id)
        if rejected:
            return rejected

        try:
            context = self._prepare_execution(username, request_id, req)
        except CommandExecutionException as e:
            return CommandExecutionResponse.failed(str(e))

        context.pipeline_cmds = self._build_pipeline(context)

        try:
            conn = await self._connect(context, req)
            context.conn = conn
        except CommandExecutionException as e:
             return CommandExecutionResponse.failed(str(e))

        if context.cmd_config.disconnects_ssh:
            return await self._handle_fire_and_forget(context)

        return await self._handle_async_execution(context)

    async def kill_command(self, command_id: str):
        """Terminate a running command using two-phase PGID-based kill.

        Phase 1: ``kill -TERM -{pgid}`` (soft kill, allows graceful shutdown).
        Phase 2: After a 2-second grace period, ``kill -KILL -{pgid}``
                 if the process group is still alive.

        Skips silently if the command_id is not found or not killable.
        """
        # 1. Try Local Kill First
        entry = _local_running_commands.get(command_id)
        if entry:
            if not entry.killable:
                logger.warning(f"Command {command_id} is not killable.", extra={"command_id": command_id})
                return
            await self._do_kill_via_connection(entry.conn, entry.pgids, command_id)
            return

        # 2. Try Cross-Pod Kill via Repository
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException:
            return  # Record not found

        if not state.is_running:
            return

        if not state.killable:
            logger.warning(f"Command {command_id} is not killable.", extra={"command_id": command_id})
            return
            
        if not state.pgids:
            return # No processes assigned yet

        logger.info(f"Initiating cross-pod kill for {command_id} on {state.host}:{state.port}")
        
        ssh_config = self._load_ssh_config(state.ssh_config)
        authenticator = create_authenticator(ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()
        
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(host=state.host, port=state.port, username=state.username, **conn_kwargs),
                timeout=10
            )
            try:
                await self._do_kill_via_connection(conn, state.pgids, command_id)
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

    async def shutdown_gracefully(self):
        """Kill all active commands during application shutdown.

        Called by the FastAPI lifespan handler to ensure no orphan processes
        remain on remote hosts after the API server stops.
        """
        logger.info(f"Shutting down {len(_local_running_commands)} running commands gracefully.")
        tasks = [self.kill_command(cmd_id) for cmd_id in list(_local_running_commands.keys())]
        if tasks:
            await asyncio.gather(*tasks)
