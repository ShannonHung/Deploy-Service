from dataclasses import dataclass, field
from typing import Optional, List
import asyncio
import asyncssh
from pydantic import BaseModel
from app.api.v1.schemas.command import CommandExecutionRequest, CommandWhitelistConfig

class SSHConnectionConfig(BaseModel):
    host: str
    port: int = 22
    username: str
    auth_method: str
    key_base64: str
    cert_base64: Optional[str] = None

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
