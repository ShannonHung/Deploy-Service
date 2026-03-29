from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import asyncio
import asyncssh
from pydantic import BaseModel, Field


# ── Whitelist Configuration ──────────────────────────────────────────────────

class CommandArgumentConfig(BaseModel):
    name: str
    type: str  # e.g., "int", "string"
    validation_regex: str = ""

class PipelineStep(BaseModel):
    command: List[str]

class CommandWhitelistConfig(BaseModel):
    command_name: str
    description: str = ""
    disconnects_ssh: bool = False
    killable: bool = False
    pipeline: List[PipelineStep]
    arguments: List[CommandArgumentConfig] = []

class UserCommandWhitelist(BaseModel):
    name: str = "admin"
    allow_hosts: List[str] = [".*"]
    deny_hosts: List[str] = []
    allow_commands: List[CommandWhitelistConfig]


# ── SSH Configuration ────────────────────────────────────────────────────────

class SSHConnectionConfig(BaseModel):
    auth_method: str
    key_base64: str
    cert_base64: Optional[str] = None


# ── Request / Response ───────────────────────────────────────────────────────

class CommandOption(BaseModel):
    timeout_seconds: int = 30

class CommandExecutionRequest(BaseModel):
    command_name: str
    host: str
    port: int = 22
    username: str
    ssh_config: str = "default"
    option: Optional[CommandOption] = Field(default_factory=CommandOption)
    arguments: Dict[str, Any] = Field(default_factory=dict)

class CommandExecutionResponse(BaseModel):
    command_id: Optional[str] = None
    status: str
    message: str = ""
    exit_status: Optional[int] = None
    output: Optional[str] = None

    @classmethod
    def failed(cls, message: str, exit_status: Optional[int] = None, output: Optional[str] = None, command_id: Optional[str] = None) -> "CommandExecutionResponse":
        return cls(status="failed", message=message, exit_status=exit_status, output=output, command_id=command_id)

    @classmethod
    def success(cls, command_id: str, exit_status: int, output: str) -> "CommandExecutionResponse":
        return cls(status="success", command_id=command_id, exit_status=exit_status, output=output)


# ── Runtime Dataclasses ──────────────────────────────────────────────────────

@dataclass
class RunningCommandEntry:
    host_ip: str
    killable: bool
    conn: Optional[asyncssh.SSHClientConnection] = None
    task: Optional[asyncio.Task] = None
    processes: List[asyncssh.SSHClientProcess] = field(default_factory=list)
    pgids: List[int] = field(default_factory=list)

@dataclass
class ExecutionContext:
    username: str
    request_id: str
    command_name: str
    raw_request: CommandExecutionRequest
    cmd_config: CommandWhitelistConfig
    ssh_config: SSHConnectionConfig
    conn: Optional[asyncssh.SSHClientConnection] = None
    pipeline_cmds: List[List[str]] = field(default_factory=list)
