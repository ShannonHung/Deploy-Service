from dataclasses import dataclass, field
from typing import Optional, List
import asyncio
import asyncssh
from pydantic import BaseModel

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
