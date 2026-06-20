from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import asyncio
from enum import Enum
import asyncssh
from pydantic import BaseModel, Field


# ── State Machine Domain Models ──────────────────────────────────────────────

class CommandStatus(str, Enum):
    RUNNING = "running"
    KILLING = "killing"
    KILLED = "killed"
    SUCCESS = "success"
    FAILED = "failed"

class HostType(str, Enum):
    IP = "ip"
    BASTION = "bastion"
    HOSTNAME = "hostname"

class CommandState(BaseModel):
    command_id: str
    status: CommandStatus
    output: Optional[str] = None
    exit_code: Optional[int] = None
    message: Optional[str] = None

    # execution metadata
    host: str
    host_type: HostType = HostType.IP
    resolved_ip: str
    port: int
    username: str
    ssh_config: str
    request_id: str
    exec_command: str

    # control
    killable: bool
    pgids: List[int] = Field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.status == CommandStatus.RUNNING

    @property
    def is_killable_state(self) -> bool:
        return self.status == CommandStatus.RUNNING

    def mark_success(self, exit_code: int, output: str):
        self.status = CommandStatus.SUCCESS
        self.exit_code = exit_code
        self.output = output

    def mark_failed(self, message: str):
        self.status = CommandStatus.FAILED
        self.message = message

    def mark_killing(self, message: str):
        self.status = CommandStatus.KILLING
        self.message = message

    def mark_killed(self, message: str):
        self.status = CommandStatus.KILLED
        self.message = message


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
    bastion_type: Optional[str] = None  # None → fall back to settings.BASTION_DEFAULT_TYPE
    ip_label: Optional[str] = None  # None → use settings.INVENTORY_IP_LABEL

class CommandExecutionRequest(BaseModel):
    command_name: str
    host: str
    host_type: HostType = HostType.IP
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
    exec_command: Optional[str] = None
    # Populated only by GET /command/execution/{id}; surfaced from CommandState.
    host_type: Optional[HostType] = None
    resolved_ip: Optional[str] = None
    pgids: List[int] = Field(default_factory=list)

    @classmethod
    def failed(cls, message: str, exit_status: Optional[int] = None, output: Optional[str] = None, command_id: Optional[str] = None) -> "CommandExecutionResponse":
        return cls(status=CommandStatus.FAILED.value, message=message, exit_status=exit_status, output=output, command_id=command_id)

    @classmethod
    def success(cls, command_id: str, exit_status: int, output: str) -> "CommandExecutionResponse":
        return cls(status=CommandStatus.SUCCESS.value, command_id=command_id, exit_status=exit_status, output=output)


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
    resolved_host: "ResolvedHost"  # forward-ref to avoid circular import
    conn: Optional[asyncssh.SSHClientConnection] = None
    pipeline_cmds: List[List[str]] = field(default_factory=list)
