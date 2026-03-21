import asyncio
import json
import logging
import re
import uuid
import os
import asyncssh
from typing import Dict, Any, List, Optional

from app.api.v1.schemas.command import CommandExecutionRequest, CommandExecutionResponse, UserCommandWhitelist, CommandWhitelistConfig
from app.core.config import get_settings
from app.domain.command import SSHConnectionConfig, RunningCommandEntry
from app.repositories.ssh_auth_repository import create_authenticator

logger = logging.getLogger(__name__)
settings = get_settings()

running_commands_pool: Dict[str, RunningCommandEntry] = {}
command_results_pool: Dict[str, CommandExecutionResponse] = {}

class CommandExecutionException(Exception):
    pass

class CommandService:
    @staticmethod
    def _validate_anti_injection(user_input: str):
        dangerous_chars = [";", "&", "|", "$", "`"]
        if any(char in user_input for char in dangerous_chars):
            raise CommandExecutionException("Invalid characters detected in input.")

    @staticmethod
    def _load_user_whitelist(username: str) -> UserCommandWhitelist:
        file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"allow-commands-{username}.json")
        if not os.path.exists(file_path):
            raise CommandExecutionException("User whitelist configuration not found. No permission.")
        with open(file_path, "r") as f:
            data = json.load(f)
        return UserCommandWhitelist(**data)

    @staticmethod
    def _load_ssh_config(target: str) -> SSHConnectionConfig:
        file_path = os.path.join(settings.COMMAND_CONFIG_DIR, f"SSH-{target}.json")
        if not os.path.exists(file_path):
            file_path = os.path.join(settings.COMMAND_CONFIG_DIR, "SSH-default.json")
            if not os.path.exists(file_path):
                 raise CommandExecutionException("SSH configuration not found.")
        with open(file_path, "r") as f:
            data = json.load(f)
        return SSHConnectionConfig(**data)

    @staticmethod
    def get_command_execution_result(command_id: str) -> CommandExecutionResponse:
        if command_id in running_commands_pool:
            return CommandExecutionResponse(status="running", command_id=command_id)
        if command_id in command_results_pool:
            return command_results_pool[command_id]
        raise CommandExecutionException(f"Execution record {command_id} not found.")

    @staticmethod
    def get_user_commands(username: str) -> UserCommandWhitelist:
        return CommandService._load_user_whitelist(username)

    @staticmethod
    def get_command_info(username: str, command_name: str) -> CommandWhitelistConfig:
        whitelist = CommandService._load_user_whitelist(username)
        cmd_config = next((c for c in whitelist.allow_commands if c.command_name == command_name), None)
        if not cmd_config:
            raise CommandExecutionException(f"Command '{command_name}' not found.")
        return cmd_config

    @staticmethod
    def _validate_and_build_pipeline(req: CommandExecutionRequest, whitelist: UserCommandWhitelist) -> CommandWhitelistConfig:
        cmd_config = next((c for c in whitelist.allow_commands if c.command_name == req.command_name), None)
        if not cmd_config:
            raise CommandExecutionException(f"Command '{req.command_name}' not found in whitelist.")

        # Validate arguments
        for arg_conf in cmd_config.arguments:
            val = req.arguments.get(arg_conf.name)
            if val is None:
                raise CommandExecutionException(f"Missing required argument: {arg_conf.name}")
            
            val_str = str(val)
            CommandService._validate_anti_injection(val_str)

            if arg_conf.validation_regex:
                if not re.match(arg_conf.validation_regex, val_str):
                    raise CommandExecutionException(f"Argument '{arg_conf.name}' does not match validation regex.")
        return cmd_config

    @staticmethod
    async def execute_command(username: str, req: CommandExecutionRequest) -> CommandExecutionResponse:
        try:
            whitelist = CommandService._load_user_whitelist(username)
            cmd_config = CommandService._validate_and_build_pipeline(req, whitelist)
            ssh_config = CommandService._load_ssh_config(req.ssh_config)
            authenticator = create_authenticator(ssh_config)
        except CommandExecutionException as e:
            return CommandExecutionResponse(status="failed", message=str(e))

        # Build pipeline execution
        # For each pipeline step, substitute {arg_name}
        pipeline_cmds = []
        for step in cmd_config.pipeline:
            resolved_step = []
            for part in step.command:
                # Replace placeholders like {time}
                for arg_conf in cmd_config.arguments:
                    placeholder = f"{{{arg_conf.name}}}"
                    if placeholder in part:
                         part = part.replace(placeholder, str(req.arguments[arg_conf.name]))
                resolved_step.append(part)
            pipeline_cmds.append(resolved_step)

        # Connect
        conn_kwargs = authenticator.get_connect_kwargs()
        try:
            conn = await asyncssh.connect(
                ssh_config.host, 
                port=ssh_config.port, 
                username=ssh_config.username, 
                **conn_kwargs
            )
        except Exception as e:
            return CommandExecutionResponse(status="failed", message=f"SSH Connection Failed: {str(e)}")

        if cmd_config.disconnects_ssh:
            # fire and forget
            try:
                import shlex
                cmd_line = pipeline_cmds[0]
                await conn.run(shlex.join(cmd_line), check=False)
            except Exception:
                pass
            finally:
                conn.close()
            return CommandExecutionResponse(status="disconnected_expected", message="Command dispatched.")

        # Create Task
        command_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        
        entry = RunningCommandEntry(
            host_ip=ssh_config.host,
            killable=cmd_config.killable,
            conn=conn
        )
        running_commands_pool[command_id] = entry

        timeout = req.option.timeout_seconds if req.option.timeout_seconds else settings.COMMAND_DEFAULT_TIMEOUT
        
        task = loop.create_task(CommandService._run_pipeline_task(command_id, conn, pipeline_cmds, timeout))
        entry.task = task

        return CommandExecutionResponse(status="running", command_id=command_id)

    @staticmethod
    async def _run_pipeline_task(command_id: str, conn, pipeline_cmds, timeout_seconds):
        entry = running_commands_pool.get(command_id)
        if not entry:
            return

        try:
            async def exec_pipe():
                processes = []
                pgids = []
                prev_stdout = None

                for i, cmd_args in enumerate(pipeline_cmds):
                    # wrapper
                    wrapper = ["bash", "-c", 'echo $$ >&2; exec setsid -w "$@"', "_"]
                    full_cmd = wrapper + cmd_args
                    
                    try:
                        import shlex
                        command_str = shlex.join(full_cmd)
                        p = await conn.create_process(
                            command_str,
                            stdin=prev_stdout,
                            stdout=asyncssh.PIPE,
                            stderr=asyncssh.PIPE
                        )
                    except Exception as e:
                        logger.error(f"Failed to create process: {e}")
                        raise
                    
                    processes.append(p)
                    # read pgid from stderr
                    pgid_str = await p.stderr.readline()
                    if pgid_str:
                        try:
                            pgids.append(int(pgid_str.strip()))
                        except Exception:
                            logger.error(f"Could not parse PGID from: {pgid_str}")
                    
                    prev_stdout = p.stdout

                entry.processes = processes
                entry.pgids = pgids

                # Wait for any prior pipeline steps to finish
                for p in processes[:-1]:
                    await p.wait()

                # Get the output from the last process via communicate to prevent EOF dropping
                output, _ = await processes[-1].communicate()
                return processes[-1].returncode, output
            
            returncode, output = await asyncio.wait_for(exec_pipe(), timeout=timeout_seconds)
            
            logger.info(f"Command {command_id} finished. Code: {returncode}")
            status = "success" if returncode == 0 else "failed"
            command_results_pool[command_id] = CommandExecutionResponse(
                status=status,
                command_id=command_id,
                exit_status=returncode,
                output=output.decode('utf-8') if isinstance(output, bytes) else str(output)
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"Command {command_id} timed out.")
            await CommandService.kill_command(command_id)
            command_results_pool[command_id] = CommandExecutionResponse(
                status="failed",
                command_id=command_id,
                message="Command timed out and was killed."
            )
        except Exception as e:
            logger.error(f"Command {command_id} failed: {str(e)}")
            command_results_pool[command_id] = CommandExecutionResponse(
                status="failed",
                command_id=command_id,
                message=str(e)
            )
        finally:
            conn.close()
            running_commands_pool.pop(command_id, None)

    @staticmethod
    async def kill_command(command_id: str):
        entry = running_commands_pool.get(command_id)
        if not entry:
            return
        
        if not entry.killable:
            logger.warning(f"Command {command_id} is not killable.")
            return

        conn = entry.conn
        pgids = entry.pgids
        
        for pgid in pgids:
            try:
                logger.info(f"Soft killing PGID {pgid}")
                await conn.run(f"kill -TERM -{pgid}", check=False)
                await asyncio.sleep(2)
                
                res = await conn.run(f"kill -0 -{pgid}", check=False)
                if res.exit_status == 0:
                    logger.info(f"Process {pgid} still running, hard killing it.")
                    await conn.run(f"kill -KILL -{pgid}", check=False)
            except Exception as e:
                logger.error(f"Error killing PGID {pgid}: {e}")

    @staticmethod
    async def shutdown_gracefully():
        logger.info(f"Shutting down {len(running_commands_pool)} running commands gracefully.")
        tasks = []
        for cmd_id in list(running_commands_pool.keys()):
            tasks.append(CommandService.kill_command(cmd_id))
        if tasks:
            await asyncio.gather(*tasks)
